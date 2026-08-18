<h1 align="center">DataLinkRuntime</h1>

<p align="center">
  一个轻量、可自托管的数据适配运行平台，用浏览器完成数据接入代码的开发、运行与运维。
</p>

<p align="center">
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml"><img src="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.13-blue" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Runtime-Python%20%7C%20JavaScript%20%7C%20Java-informational" alt="Runtimes">
</p>

<p align="center">
  <strong>简体中文</strong> · <a href="README.en.md">English</a> ·
  <a href="docs/zh-CN/product.md">产品定义</a> ·
  <a href="docs/zh-CN/architecture.md">总体架构</a> ·
  <a href="docs/specs/README.md">规格索引</a> ·
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/issues/80">路线图</a>
</p>

# DataLinkRuntime - 轻量数据适配运行平台

DataLinkRuntime（DLR）面向 CMDB 等系统的数据采集、接收、解析、转换和输出场景。

核心思路很简单：**一个 Adapter 就是一个自包含的数据处理单元**。你在 Web Workbench 中编写代码，DLR 把代码交给 Worker 执行，并为每一次运行保留可追溯的输入、输出、日志和状态。

DLR 有意保持轻量：它不是工作流引擎，也不是低代码平台，而是专注于把 Adapter 的开发、执行和运维做得简单、明确、易于私有化部署。

## 主要概念

1. **创建 Adapter**
   - `Task`：支持手动运行和定时运行。
   - `Webhook`：通过 HTTP 接收外部 JSON 请求。

2. **在浏览器中开发**
   - Monaco 在线编辑器。
   - Python、JavaScript、Java 共用统一的 `Input → handle(context, input) → Output` 合同。
   - 依赖、运行配置和 Credential Binding 跟随 Adapter 管理。

3. **在 Worker 上执行**
   - Control 负责状态、事务和调度，但不执行用户 Adapter 代码。
   - Worker 主动领取 Execution，并在独立子进程 / JVM 中运行代码。
   - 同一个 Adapter 同时最多只有一个 `pending / running` Execution。

4. **观察每一次运行**
   - 实时 stdout / stderr。
   - 结构化 Output。
   - Execution 历史与 Webhook 调用记录。
   - Task、Schedule、Webhook 共用超时、取消与运行锁语义。

5. **用 AI 辅助开发，但保留人工确认**
   - AI Assistant 读取当前 Working Copy 和有界的非敏感上下文。
   - AI 返回完整 Candidate 快照。
   - 管理员先看 Diff，再明确 Apply。
   - Apply 不会自动保存、测试或运行 Adapter。

## 功能特性

- 基于 Monaco Editor 的浏览器 Adapter Workbench。
- Task 手动运行与 Cron / Timezone 定时运行。
- 带 Bearer Token 鉴权的 Webhook Adapter。
- Python 3.13、JavaScript / Node.js、Java 21 Runtime。
- Version-scoped 依赖环境。
- PostgreSQL 驱动的 Execution 状态与调度。
- Worker 心跳与 capability 感知调度。
- 实时日志、Execution 历史、Webhook 调用记录。
- 加密 Credential 与 Adapter Secret Binding。
- Python / npm / Maven 依赖源管理。
- Human-in-the-loop AI Assistant：Candidate → Diff → Apply。
- 部署级 `zh-CN / en` 国际化。
- Docker Compose 私有化部署。

## 快速开始

前置条件：Docker，并支持 Compose v2。

创建部署配置并替换其中的占位凭据：

```bash
cp .env.example .env
# 编辑 .env，至少设置：
# DLR_ADMIN_TOKEN
# DLR_WORKER_TOKEN
# DLR_MASTER_KEY
```

先启动 PostgreSQL 并执行数据库迁移：

```bash
docker compose up -d postgres
docker compose run --rm control alembic upgrade head
```

启动完整环境：

```bash
docker compose up -d --build
```

检查服务状态：

```bash
docker compose ps
```

全部服务健康后：

- Web Console：`http://localhost:8080`
- Health API：`http://localhost:8080/api/health`

首次进入 Web Console 时需要输入 `DLR_ADMIN_TOKEN`；Token 只保存在浏览器 `sessionStorage` 中。

清理本地环境及数据库卷：

```bash
docker compose down --volumes
```

## 架构

```text
┌─────────────────────┐
│ Web                  │
│ React + Monaco       │
└──────────┬──────────┘
           │ HTTP/JSON + SSE
           ▼
┌─────────────────────┐       ┌─────────────────────┐
│ Control              │──────▶│ PostgreSQL          │
│ FastAPI              │       │ 状态 / 调度         │
│ API / 门禁 / AI      │       │ 执行历史            │
└──────────┬──────────┘       └─────────────────────┘
           │ Worker 主动长轮询
           ▼
┌─────────────────────┐
│ Worker               │
│ Python / Node / Java │
│ 独立子进程执行       │
└─────────────────────┘
```

DLR 刻意保持清晰的执行边界：

- **Web**：提供管理员操作体验。
- **Control**：负责 API、事务门禁、调度、Webhook 路由和 AI Provider 集成。
- **PostgreSQL**：保存权威持久化状态。
- **Worker**：唯一真正执行用户 Adapter 代码的组件。

当前详细合同见 [总体架构](docs/zh-CN/architecture.md)。

## 技术栈

| 层 | 技术 |
|---|---|
| Web | React 19、TypeScript、Vite、Ant Design、Monaco Editor、i18next |
| Control | Python 3.13、FastAPI、SQLAlchemy 2、Alembic |
| Database | PostgreSQL 16 |
| Worker | Python、Node.js / npm、JDK 21 / Maven |
| Python 工具链 | uv、pytest、Ruff、mypy |
| 部署 | Docker Compose |

## AI Assistant

当前 AI Assistant 是一个**受约束的编程助手**，不是自主 Agent。

```text
Working Copy + 有界上下文
→ 模型回答
→ Candidate 严格校验
→ Diff 审阅
→ 人工明确 Apply
→ 浏览器 Working Copy 进入 dirty
```

安全与行为边界：

- Working Copy 是本轮请求的权威代码快照。
- Credential 真值不会进入 Prompt。
- 只允许把已绑定 Secret 的 key 名称提供给模型，帮助生成可运行代码。
- Provider reasoning 不持久化、不展示。
- AI 对话、Prompt、Provider 原始响应不持久化。
- Apply 不会自动执行 Save / Test / Run。

M5.7 正在继续扩展 AI Assistant：采用 `assistant-ui`、加入 Regenerate、附件、受控只读 Tool Call 和 MCP 知识接入。在实现并完成人工验收之前，这些能力属于路线图而不是当前已完成能力。详见 [Issue #80](https://github.com/john-ops-lab/DataLinkRuntime/issues/80)。

## 安全

- Credential 使用部署级 `DLR_MASTER_KEY` 派生密钥进行静态加密。
- Credential 明文永远不会返回浏览器。
- Runtime Secret 只为目标 Execution 注入，并从平台日志中脱敏。
- Admin API 与 Worker API 使用不同的 Bearer Token。
- Webhook Token 使用 constant-time compare。
- Control 永远不执行用户 Adapter 代码。
- DLR v1 采用可信管理员代码模型；子进程隔离**不等于安全沙箱**。

不要在 Adapter 源码中硬编码密码、Token、私钥或其他 Secret。

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

## 文档

- [产品定义](docs/zh-CN/product.md)
- [总体架构](docs/zh-CN/architecture.md)
- [规格索引与冲突优先级](docs/specs/README.md)
- [M5.7 AI Assistant Spec](docs/specs/m5-7-ai-assistant.md)

M1-M4 的历史 Spec 会继续保留用于追溯。当文档发生冲突时，应遵循 `docs/specs/README.md` 中定义的优先级，而不是把所有历史 Spec 都当成当前产品行为。

## 路线图

当前阶段：**M5.7 - AI Assistant UI 组件化与受控知识接入扩展**。

计划范围包括：

- 基于 `assistant-ui` 的 Chat UI。
- Regenerate。
- 图片、PDF、Word、文本和代码附件。
- Provider 原生文件 / 多模态能力优先，DLR 提供有界 fallback 解析。
- 只读 Tool Call。
- MCP 知识接入，腾讯 ima 知识库作为首个 POC。

M5.7 明确不做 Streaming token 输出、Reasoning UI 和通用自主 Agent Runtime。

当前合同见 [Issue #80](https://github.com/john-ops-lab/DataLinkRuntime/issues/80)。

## License

DataLinkRuntime 基于 [MIT License](LICENSE) 开源。

Copyright (c) 2026 john-ops-lab。
