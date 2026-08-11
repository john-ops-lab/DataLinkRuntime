# DataLinkRuntime (DLR)

轻量的数据适配运行平台，用于 CMDB 等系统的数据采集、接收、解析、转换和输出。

- 产品定义：[docs/product.md](docs/product.md)
- 总体架构：[docs/architecture.md](docs/architecture.md)

当前处于 M0（工程骨架）阶段：四容器可运行 + Health Check，尚未实现 Adapter 管理与执行能力。

## 快速开始

前置条件：Docker（含 Compose v2）。

```bash
docker compose up --build
```

服务健康后：

- Web UI：http://localhost:8080
- Control Health（经 web/nginx）：http://localhost:8080/api/health
- Worker：无对外端口（M0 为常驻 agent 存根，healthcheck 基于 ready 文件）

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
| worker | Worker Agent 存根（M2 起承接 Adapter 执行） |

## 本地开发

### backend

前置条件：[uv](https://docs.astral.sh/uv/)（会自动安装 Python 3.13）。

```bash
cd backend
uv sync --frozen
uv run uvicorn dlr.control.app:create_app --factory --reload
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

数据库迁移（M0 为空骨架，迁移文件自 M1 产生；PostgreSQL 仅在 Compose 内部网络可达）：

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

构建并启动全部容器，验证健康状态与 `/api/health` 链路后自动清理：

```bash
./scripts/compose-smoke.sh
```
