# DataLinkRuntime (DLR)

轻量的数据适配运行平台，用于 CMDB 等系统的数据采集、接收、解析、转换和输出。

- 产品定义：[docs/product.md](docs/product.md)
- 总体架构：[docs/architecture.md](docs/architecture.md)
- M4 AI Editor Spec：[docs/specs/m4-ai-editor.md](docs/specs/m4-ai-editor.md)

当前已完成 M5.4 Workbench：Python / JavaScript / Java Adapter 共享不可变 Revision、
Secret、实时日志与 Execution 历史语义，并可通过右侧
AI Assistant 基于当前 Working Copy 生成完整 Candidate。Candidate 必须经管理员查看 Diff
并明确应用，应用只更新浏览器 Working Copy；保存与运行仍全部由管理员执行。Task Adapter
支持手动或定时运行，两种入口始终执行最新已保存内容，并固定到配置的运行节点。
Webhook Adapter 创建时自动获得随机 URL path，配置 Token Credential 与运行节点后可开启接收；
每个成功 JSON 请求异步创建一条 Execution，并按 Adapter 只保留最近 100 条
Webhook 调用记录。停止接收会立即拒绝新请求，但不会终止已经在执行的调用。
Task、Schedule 与 Webhook 复用统一运行锁和 Workbench 底部实时日志；日志支持全屏与恢复。
运行中代码、运行配置、保存与删除会锁定，并提供 Clone 升级入口和明确原因。
官方 Worker 镜像包含 Python 3.13 / uv、Node.js LTS / npm、JDK 21 / Maven，
并按实际可用 Runtime 自动上报 capability。
M4.1 进一步以心跳超时派生 Worker 的有效在线状态，避免运行入口选择已经失联的
Worker。

## 快速开始

前置条件：Docker（含 Compose v2）。

M2 起 Control / Worker 需要静态 Token；M3.2 的 Secret Store 还需要部署级
`DLR_MASTER_KEY`。Compose 不为这些值内置任何可用默认值：

```bash
cp .env.example .env   # 修改 DLR_ADMIN_TOKEN / DLR_WORKER_TOKEN / DLR_MASTER_KEY 等占位值
docker compose up -d --build
```

首次启动时 Worker 需要 `workers` 表才能注册，因此必须先运行数据库迁移：

```bash
# 等待 PostgreSQL 启动
docker compose ps postgres
docker compose run --rm control alembic upgrade head
```

迁移完成后等待全部服务健康：

```bash
docker compose ps
```

所有服务状态为 `healthy` 后：

- Web UI：http://localhost:8080（首次进入需输入 `DLR_ADMIN_TOKEN`，仅存于浏览器 sessionStorage）
- Control Health（经 web/nginx）：http://localhost:8080/api/health
- Worker：无对外端口（出站长轮询 Control，healthcheck 基于 ready 文件）

管理员 API 需 `Authorization: Bearer <DLR_ADMIN_TOKEN>`；Worker API 需
`DLR_WORKER_TOKEN`。Runtime Secret 以 `DLR_SECRET_*` 形式只注入 Worker，
Adapter 通过 `context.secrets.get(...)` 读取。

清理环境（含数据库卷）：

```bash
docker compose down --volumes
```

## 组件

| 组件 | 说明 |
|------|------|
| web | React + TypeScript + Vite SPA，由 Nginx 托管并反代 `/api` |
| control | FastAPI 控制节点（Python 3.13） |
| postgres | PostgreSQL 16 |
| worker | Worker Agent：注册/心跳/长轮询，按语言在 version-scoped 环境的独立子进程中执行 Adapter |

## Worker 有效在线状态

Worker 默认每 10 秒发送一次心跳（`DLR_WORKER_HEARTBEAT_SECONDS`）。数据库中的
`workers.status` 是 Worker 在 register / heartbeat / graceful offline 时主动写入的
Stored Status；Control 不通过后台任务把心跳过期记录改写为 `offline`。

Control 在需要判断 Worker 当前是否可用时派生 Effective Status：Stored Status 必须为
`online`，且最近心跳年龄必须小于等于
`DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS`（默认 30 秒）。恰好位于超时边界仍视为在线。
Admin Worker API 的 `status`、Test 与 Start 都使用该有效状态；`last_heartbeat` 原样返回，
仅用于排障，Web 不使用浏览器时间重复计算。

心跳超时值必须为正数并严格大于 Worker 心跳间隔；部署方调整心跳间隔时，应同步调整
超时值，建议不少于心跳间隔的约 3 倍。过期 Worker 恢复心跳后会自动恢复有效在线，
无需人工干预。

## AI Assistant 配置与边界

AI Assistant 使用一个全局活动模型配置。在「系统设置 → AI 模型」中选择 Provider，填写
Base URL 与 Model ID；Model ID 既可从 Provider 的 `/v1/models` 刷新，也可手工输入。如需
API Key，先创建 `token` 类型 Credential，再在 AI 设置中引用；浏览器与 AI 设置 API
只看到 Credential 元数据，不会得到 token 明文。推理策略默认「跟随模型默认」，此时
DLR 不主动发送 reasoning override。

非流式 Provider HTTP 请求默认超时为 180 秒，可通过
`DLR_AI_PROVIDER_TIMEOUT_SECONDS` 在 10～600 秒范围内调整；该参数仅控制请求 deadline，
不新增 streaming 或输出 token 参数管理。

管理员配置的模型服务会收到当前 Working Copy、非敏感运行参数、已绑定 Secret 的
`env_key` 名称以及有限的最近对话；不会收到这些业务 Credential 的真值。所选模型 API
Key 只作为 Provider HTTP Authorization 使用，不进入 Prompt。对话、Prompt、Provider
Response 与 reasoning 不落库、不写普通应用日志。模型返回的 Candidate 经本地 Schema
校验后仍需人工 Apply，且 Apply 不调用任何生命周期 API。

## 本地开发

### backend

前置条件：[uv](https://docs.astral.sh/uv/)（会自动安装 Python 3.13）。

```bash
cd backend
uv sync --frozen
uv run uvicorn dlr.control.app:create_app --factory --reload
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest   # 需要可达的 PostgreSQL（测试自建独立的 dlr_test 库并执行真实迁移）
```

本地无可用 PostgreSQL 时，可临时启动一个测试实例（默认 `DATABASE_URL` 即可直连）：

```bash
docker run --rm -d --name dlr-dev-pg -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=dlr -e POSTGRES_PASSWORD=dlr -e POSTGRES_DB=dlr postgres:16
```

数据库迁移（PostgreSQL 仅在 Compose 内部网络可达）：

```bash
docker compose run --rm control alembic upgrade head
```

### web

前置条件：Node.js 22+。

```bash
cd web
npm ci
npm run dev        # http://localhost:5173，/api 代理到 localhost:8000
npm run lint
npm run typecheck
npm run test
npm run build
```

## 冒烟测试

构建并启动全部容器（隔离的 compose project 与独立端口），在真实 PostgreSQL
上执行 Alembic 迁移后等待全部服务健康，验证 `/api/health` 链路与 401 认证拒绝。
M5.4 Task 主链路真实跑通 `Task create → Save + Worker → Run Once → succeeded →
切换定时运行 → 配置短周期 Schedule → enable → schedule Execution succeeded → disable`，
并验证 Python / JavaScript / Java 的真实基础执行均输出“任务开始/任务结束”、Run Once 与
Schedule 都固定最新已保存内容/运行 Worker、统一活跃锁和 Clone 后 Schedule disabled。
Webhook 主链路真实跑通 `create → Save + Worker + Token → 可读 path → 开启接收 → POST JSON
→ 202 → succeeded → 调用记录 → 停止接收`，并验证停止不终止 active Execution、同 path 的
运行中唯一约束，以及 `Clone disabled → 旧 Adapter 停止 → Clone 接管原 URL`。M4 链路
额外启动一个临时、仅位于 smoke 网络中的 OpenAI-compatible fake Provider，验证设置、
模型刷新、连接测试与三语言 AI Assist，并证明 AI 不改变保存、Execution 或运行配置事实；
不访问任何公网 AI，也不把 fake Provider 加入正式 Compose 拓扑。整个过程
使用独立 Compose project 和卷，结束后自动清理：

```bash
./scripts/compose-smoke.sh
```

## 容器网络与 DNS 排障

### 默认行为

平台不在 Compose 配置中硬编码任何机器特定 DNS。`control` / `worker` 的
出站网络走 Compose 内置 DNS（容器内 `127.0.0.11`），由 Docker 转发到宿主
机 `resolv.conf` 的配置；fresh deployment 无需了解内部细节即可启动。只有
两类外部连接需要域名解析：

- `control` 访问 AI Provider 的 Base URL（AI 设置中的模型服务）；
- `worker` 按 Adapter 语言下载依赖（PyPI / npm / Maven，兼容配置在
  `.env.example` 中可选填写）。

### 何时需要覆盖 DNS

仅当容器内无法解析外部域名（企业网络 / VPN / 防火墙拦截了 Docker 的 DNS
转发）时，才需要显式覆盖。覆盖是可选、按部署定制的，不修改
`docker-compose.yml`：

```bash
cp docker-compose.dns.example.yml docker-compose.dns.yml
# 编辑 docker-compose.dns.yml，把占位地址替换为你所在网络实际可用的 DNS
docker compose -f docker-compose.yml -f docker-compose.dns.yml up -d --build
```

### 分层检查顺序（DNS → TCP → TLS/HTTP）

排障时按层级从下往上定位，不要跳过：

1. **DNS 解析失败**：AI 设置返回错误码 `ai_provider_dns_failed`（文案含
   「域名解析失败」）。在容器内直接验证解析：
   ```bash
   docker compose exec control python -c "import socket; socket.getaddrinfo('api.example.com', 443)"
   ```
   失败说明 DNS 层故障：优先使用上面的 DNS 覆盖文件；企业网络请确认该
   DNS 允许出站解析，VPN 请确认路由未拦截 DNS 流量。
2. **TCP 连接失败**：错误码 `ai_provider_unreachable`（文案含
   「TCP 连接或 TLS 握手失败」）。验证到目标端口的三次握手：
   ```bash
   docker compose exec control python -c \
     "import socket; socket.create_connection(('api.example.com', 443), timeout=5)"
   ```
   失败说明网络层故障：检查容器出站防火墙 / 代理 / VPN 路由，与 DNS 无关。
3. **TLS / HTTP 失败**：仍在 `ai_provider_unreachable`（TLS 握手失败）或
   其他错误码（`ai_auth_failed` 表示凭据被拒绝、`ai_model_not_found` 表示
   模型 ID 不存在、`ai_timeout` 表示请求超时）。这一步说明网络与解析都已
   正常，问题在服务端接口、证书链或鉴权。

上述三个层级可以在容器内（与 `control` 相同的 DNS 环境）一次跑完：

```bash
# 在 control 容器内运行分层诊断（DNS → TCP → TLS → HTTP）
docker compose exec -T control python - < scripts/diag-network.py --url https://api.example.com
```

脚本按层级停止并输出失败层与退出码（2=DNS、3=TCP、4=TLS、5=HTTP）；
宿主机上也可直接 `python3 scripts/diag-network.py --host api.example.com --port 443`
（`--host` 模式只检查 DNS/TCP，加 `--tls` 可额外检查 TLS 握手；不做 HTTP 探测）。

### Docker Desktop / VPN / 企业网络检查清单

- Docker Desktop：确认「Settings → Resources → Network」未限制出站，且
  宿主 DNS 正常（`scutil --dns` / `nslookup` 能解析目标域名）。
- VPN：确认 VPN 未把 Docker 虚拟机流量全部黑洞（可临时断开 VPN 复现对比）；
  若 VPN 自带 DNS，把该 DNS 写入 `docker-compose.dns.yml`。
- 企业网络：确认代理配置；`control` 出站不走宿主代理环境变量（镜像内
  `HTTP_PROXY` 未设置），如有企业代理要求请在部署层注入并保持平台
  配置文件中不含代理凭据。
- 平台自身不暴露 Token / Credential / Provider API Key：所有诊断命令只
  使用域名、端口与 URL，绝不读取或回显密钥。
