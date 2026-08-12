# DataLinkRuntime (DLR)

轻量的数据适配运行平台，用于 CMDB 等系统的数据采集、接收、解析、转换和输出。

- 产品定义：[docs/product.md](docs/product.md)
- 总体架构：[docs/architecture.md](docs/architecture.md)

当前处于 M2（执行闭环）阶段：四容器可运行 + Health Check + Adapter/版本管理 + Admin/Worker Token 认证 + Manual Execution 由 Worker 在 version-scoped venv 子进程中真实执行。

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
| worker | Worker Agent：注册/心跳/长轮询领取任务，在 version-scoped venv 子进程中执行 Adapter |

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
上执行 Alembic 迁移后等待全部服务健康，验证 `/api/health` 链路与 401 认证拒绝，
带 Admin Token 执行 M1 Adapter 管理完整链路（创建 → 修改 → 保存 v1/v2 →
发布历史版本 → 版本列表/详情 → 删除 → 404），再执行 M2 执行闭环（创建
Manual Execution → Worker 领取 → 建 venv → 子进程执行 → 轮询至 succeeded →
校验 input/runtime_config/output 与 Secret 可用性 → 校验有执行记录的 Adapter
不可删除 → 校验 Worker runtime 卷中的 .venv/.ready）后自动清理：

```bash
./scripts/compose-smoke.sh
```
