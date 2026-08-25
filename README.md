<h1 align="center">DataLinkRuntime</h1>

<p align="center">
  <strong>Code your data connections.</strong>
</p>

<p align="center">
  把数据连接逻辑写成 <strong>Adapter（适配器）</strong>，从代码编辑、依赖配置到执行、日志与历史追踪，<br>
  DataLinkRuntime 提供一个轻量、自托管的数据适配开发与运行环境。
</p>

<p align="center">
  <strong>Develop → Run → Observe</strong>
</p>

<p align="center">
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml"><img src="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.13-blue" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Runtime-Python%20%7C%20JavaScript%20%7C%20Java-informational" alt="Runtimes">
</p>

<p align="center">
  <strong>简体中文</strong> ·
  <a href="README.en.md">English</a> ·
  <a href="docs/zh-CN/product.md">产品定义</a> ·
  <a href="docs/zh-CN/architecture.md">总体架构</a> ·
  <a href="docs/specs/README.md">规格索引</a> ·
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/issues">Issues</a>
</p>

---

## DataLinkRuntime 是什么？

DataLinkRuntime（DLR）是一个 **AI 辅助、代码优先（Code-first）的数据适配开发与运行平台**。

很多数据连接任务，本质上只是一段并不复杂的代码：

```text
读取 / 接收数据
      ↓
校验与转换
      ↓
处理业务逻辑
      ↓
输出到目标系统
```

代码本身可能只有几十到几百行，但一旦需要长期运行，就会同时带来依赖、配置、凭据、调度、版本、日志、执行历史和故障排查等问题。

DLR 把这些能力统一收进一个轻量平台。每个 **Adapter** 都是一个自包含的数据处理单元：

```text
Source / Event
      ↓
   Adapter
      ↓
Transform / Process
      ↓
    Target
```

Adapter 自己负责一次完整的数据处理和外部输出逻辑，DLR 负责它的开发、运行与管理。

---

## Code-first, AI-assisted

很多数据集成平台通过组件、节点和流程图来降低开发门槛。DataLinkRuntime 选择另一条路径：

> **不把逻辑拆成越来越多的平台组件，而是让 AI 帮助你直接生成和修改可读、可运行的 Adapter 代码。**

你可以自己写代码，也可以直接描述需求：

```text
Describe → Generate → Review → Run
```

DLR 的 AI Assistant 生成 Candidate，由用户查看 Diff 后明确 Apply；Apply 只修改浏览器中的 Working Copy，不会自动保存或运行 Adapter。

代码仍然是最终资产，因此可以直接阅读、修改、测试和版本化，也可以继续使用 Python、JavaScript、Java 现有生态。

**DLR 不试图用更多组件消灭代码，而是用 AI 让代码重新成为简单的表达方式。**

---

## 应用场景

DLR 适合那些 **逻辑相对独立，但需要稳定运行和长期维护的数据连接任务**。

- **系统数据同步**：从系统 A 获取数据，转换后写入系统 B。
- **API / 数据格式适配**：字段映射、枚举转换、结构重组、数据清洗与协议差异处理。
- **数据采集**：定时采集云平台、Kubernetes、数据库、监控或业务系统数据。
- **Webhook / 事件处理**：接收 GitHub、CI/CD、监控、云平台或业务系统事件并转换、转发。
- **定时任务与脚本收敛**：把散落在服务器、Cron、容器或个人目录里的脚本统一纳入运行、日志和版本管理。
- **一次性数据处理**：数据迁移、批量修正、临时转换和短期接口任务。

常见处理模式：

```text
Fetch → Transform → Push
```

或：

```text
Receive → Validate → Process → Send
```

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **Web Workbench** | 在浏览器中创建、编辑和管理 Adapter |
| **多语言 Runtime** | Python、JavaScript、Java 使用统一运行模型 |
| **Task** | 支持手动运行和 Cron / Timezone 定时执行 |
| **Webhook** | 接收外部 HTTP 请求并创建 Execution |
| **依赖管理** | 管理 Python、npm、Maven 依赖及依赖源 |
| **Credential** | 加密保存凭据，并通过 Secret Binding 注入运行环境 |
| **版本留痕** | 每次保存形成不可变的运行快照 |
| **实时日志** | 实时查看 Adapter 运行日志 |
| **执行记录** | 查看 Execution 状态、耗时、输入、输出与日志 |
| **Worker Runtime** | Control 与代码执行分离，由 Worker 实际运行 Adapter |
| **AI Assistant** | Candidate → Diff → Apply 的 Human-in-the-loop 代码辅助 |
| **AI Context** | 支持显式代码 / 日志上下文、附件和受控只读知识源 |
| **自托管** | 一台服务器 + Docker Compose 即可部署 |
| **国际化** | 提供简体中文与 English 界面 |

---

## Adapter Runtime Contract

三种语言共享同一个核心模型：

```text
Input → handle(context, input) → Output
```

| 语言 | 入口 |
|---|---|
| Python | `def handle(context, input)` |
| JavaScript | `export async function handle(context, input)` |
| Java | `Adapter.handle(Context context, Object input)` |

运行时提供：

- `context.config`：非敏感运行参数；
- `context.secrets.get(key)`：读取已绑定 Secret；
- `context.logger`：输出实时日志；
- `input`：JSON 兼容输入；
- `output`：JSON 可序列化输出。

Adapter 可以根据需要调用数据库、HTTP API、SDK 或其他外部系统。

---

## Adapter 类型

### Task

用于主动执行的数据处理任务，支持：

- 手动运行；
- Cron / Timezone 定时运行；
- 自定义 Input；
- 运行 / 停止；
- 实时日志；
- 执行历史。

典型流程：

```text
Create → Edit → Save → Run / Schedule → Observe
```

### Webhook

用于接收外部系统主动推送的数据：

```text
External System
      ↓
POST Webhook
      ↓
DLR Control
      ↓
Execution
      ↓
Worker
      ↓
Adapter
```

适合 GitHub、CI/CD、监控平台、云平台和业务系统事件接入。

---

## AI Assistant

AI Assistant 是 Adapter 的开发助手，而不是自主运行 Agent。

它可以结合当前 Working Copy、用户显式加入的代码 / 日志上下文、附件，以及配置后的受控只读知识源，帮助生成、修改和解释 Adapter 代码。

```text
Working Copy + Context
        ↓
   AI Assistant
        ↓
     Candidate
        ↓
       Diff
        ↓
      Apply
        ↓
Working Copy (dirty)
```

AI 不会自动执行：

```text
Save
Run
Stop
修改 Worker
修改 Schedule / Webhook 生命周期
```

最终保存和运行始终由用户明确触发。

---

## 快速开始

### 前置条件

需要 Docker，并支持 Compose v2。

### 1. 创建部署配置

```bash
cp .env.example .env
```

至少设置：

```text
DLR_ADMIN_TOKEN
DLR_WORKER_TOKEN
DLR_MASTER_KEY
```

请使用真实随机 Secret 替换示例值。

### 2. 准备平台日志 bind mount

`.env.example` 默认使用当前用户可写的仓库内目录 `./platform-logs`，与
Linux 生产部署使用的绝对路径 `/var/lib/dlr/platform-logs` 不同。启动 Compose
前先准备五个宿主机子目录；Compose 会把它们 bind mount 到容器内固定的
`/var/lib/dlr/platform-logs/<service>/` 路径。仓库根目录的 `/platform-logs/`
已在 `.gitignore` 中精确忽略，其他路径不受此规则影响：

```bash
LOG_ROOT=./platform-logs
mkdir -p "$LOG_ROOT"/{control,worker,web,account-web,postgres}
```

五个目录分别是 `control/`、`worker/`、`web/`、`account-web/` 和 `postgres/`。
PostgreSQL 启动前会以容器内 `postgres` 用户检查 `postgres/` 是否可写；Linux
生产环境请先在固定的 pinned image 中运行 `id postgres`，只给该目录授予所需的
最小访问权限。不要使用 `chmod 777`。如果修改了 `DLR_PLATFORM_LOG_ROOT`，请在
对应的宿主机根目录下重复上述准备步骤。

平台日志是独立的 bind mount：保留现有轮转和脱敏规则，不要把 Token、Secret、
密码或其他真实凭据写入 `.env.example`、日志目录或命令输出。完整的生产路径、
轮转和权限说明见 [平台日志部署文档](docs/deployment/platform-logs.md)。

Control 的 AI 工具轨迹单独写入
`<DLR_PLATFORM_LOG_ROOT>/control/ai-tool-audit.jsonl`，由应用按大小轮转，不进入
宿主机仅匹配 `*.log` 的普通 logrotate。`DLR_AI_TOOL_AUDIT_MAX_BYTES` 默认
`10485760`（10 MiB，允许 1～104857600），`DLR_AI_TOOL_AUDIT_BACKUP_COUNT`
默认 `10`（允许 1～100）；默认最坏占用为当前文件加 10 份历史文件，即
110 MiB。单次 Assist 总时限由 `DLR_AI_ASSIST_TOTAL_TIMEOUT_SECONDS` 控制，默认
150 秒且仅允许 120～180 秒。回滚旧版 Control/Web 时移除这三个变量；审计文件
无数据库依赖，可按部署保留策略保留或删除。

### 3. 启动 PostgreSQL 并执行迁移

```bash
docker compose up -d postgres
docker compose run --rm control alembic upgrade head
```

### 4. 启动完整平台

```bash
docker compose up -d --build
```

检查状态：

```bash
docker compose ps
```

全部服务健康后访问：

- Web Console：`http://localhost:8080`
- Account Console：`http://localhost:8081`
- Health API：`http://localhost:8080/api/health`

首次进入 Web Console 时输入 `DLR_ADMIN_TOKEN`。

账号入口首次登录使用 `admin / admin123`，随后必须修改密码；密码只保存为
服务端安全 Hash。8080 Token 入口与 8081 账号入口共用同一个 Control、PostgreSQL
和 Web 构建，不复制业务数据。账号入口宿主机端口可通过
`DLR_ACCOUNT_WEB_HOST_PORT` 配置。

清理本地环境与数据库卷：

```bash
docker compose down --volumes
```

---

## 架构

```text
┌─────────────────────────────┐
│ Web                         │
│ React + Monaco + AI UI      │
└──────────────┬──────────────┘
               │ HTTP / JSON / SSE
               ▼
┌─────────────────────────────┐       ┌─────────────────────┐
│ Control                     │──────▶│ PostgreSQL          │
│ FastAPI                     │       │ State / History     │
│ API / Scheduler / AI        │       └─────────────────────┘
│ Webhook / Credential        │
└──────────────┬──────────────┘
               │ Worker Poll
               ▼
┌─────────────────────────────┐
│ Worker                      │
│ Python / Node.js / Java     │
│ Adapter Execution           │
└─────────────────────────────┘
```

职责边界：

- **Web**：提供 Adapter 开发、配置、运行和观察体验；
- **Control**：负责 API、状态、事务门禁、调度、Webhook、Credential 和 AI Provider 集成；
- **PostgreSQL**：保存平台权威状态、版本和执行历史；
- **Worker**：真正执行用户 Adapter 代码。

Control 本身不执行 Adapter 代码。

详细合同见 [总体架构](docs/zh-CN/architecture.md)。

---

## 产品边界

DataLinkRuntime 专注于 **独立 Adapter 的开发和运行**。

它不是：

- DAG / Workflow 编排引擎；
- 拖拽式低代码流程平台；
- 大规模实时流计算平台；
- 企业服务总线；
- 通用 Serverless 平台；
- 通用 AI Agent Runtime。

如果一个需求的核心已经变成多任务依赖、并行分支、复杂条件、人工审批或跨任务状态编排，它更适合使用专门的 Workflow / DAG 平台。

---

## 安全边界

DLR 当前采用 **可信代码运行模型**。

- Adapter 子进程隔离不等同于安全沙箱；
- 不应运行来自不可信用户的任意代码；
- Credential 真值不会返回浏览器；
- Secret 只为目标 Execution 按需注入，并从平台日志中脱敏；
- Control 不直接执行用户 Adapter 代码；
- 不要在 Adapter 源码中硬编码密码、Token、私钥等凭据；
- AI Assistant 不会把 Credential 真值作为正常上下文发送给模型；
- AI 附件内容会发送给管理员配置的模型服务，请勿上传密码、密钥等敏感凭据。

更多安全与运行合同见 [总体架构](docs/zh-CN/architecture.md)。

---

## 技术栈

| 层 | 技术 |
|---|---|
| Web | React 19、TypeScript、Vite、Ant Design、Monaco Editor、assistant-ui、i18next |
| Control | Python 3.13、FastAPI、SQLAlchemy 2、Alembic |
| Database | PostgreSQL 16 |
| Worker | Python、Node.js / npm、JDK 21 / Maven |
| Python 工具链 | uv、pytest、Ruff、mypy |
| 部署 | Docker Compose |

---

## 本地开发

Backend：

```bash
cd backend
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Web：

```bash
cd web
npm ci
npm run lint
npm run typecheck
npm run test
npm run build
```

完整集成冒烟测试：

```bash
./scripts/compose-smoke.sh
```

Smoke Test 使用隔离的本地环境和 fake AI Provider，不会访问公网 AI 服务。

---

## 文档

- [产品定义](docs/zh-CN/product.md)
- [总体架构](docs/zh-CN/architecture.md)
- [规格索引与冲突优先级](docs/specs/README.md)

历史 Specs 用于保留设计与演进记录。当历史文档与当前实现发生冲突时，请遵循 `docs/specs/README.md` 中定义的优先级。

发现 Bug、产品问题或有新的使用场景建议，可以通过 [GitHub Issues](https://github.com/john-ops-lab/DataLinkRuntime/issues) 提交反馈。

---

## License

DataLinkRuntime 基于 [MIT License](LICENSE) 开源。

Copyright (c) 2026 john-ops-lab
