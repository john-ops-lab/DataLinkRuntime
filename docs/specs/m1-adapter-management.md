# M1 Adapter Management — Detailed Design

> 状态：Approved for implementation  
> 基线：M0 已合并到 `main`  
> 实施分支：`feat/m1-adapter-management`  
> 范围：Adapter 管理、不可变版本、发布、Monaco 在线编辑、requirements / runtime_config  
> 非范围：Adapter 执行、Worker 心跳/任务、Execution、Runtime harness、依赖安装、Secret、Schedule、Webhook、AI

## 1. M1 目标

M1 要完成 DLR 的“Adapter 管理闭环”，让管理员能够：

```text
创建 Adapter
→ 编辑 Adapter 元数据
→ 在线编辑 Python 代码
→ 编辑 requirements / runtime_config
→ 显式保存为不可变版本
→ 查看版本历史
→ 发布任意已有版本
→ 删除 Adapter
```

M1 结束后，平台必须能够完整管理 Adapter 及其版本，但**仍然不能执行用户 Adapter 代码**。

M1 不改变 `docs/product.md` 和 `docs/architecture.md` 已确认的产品与架构边界。

---

## 2. 本阶段必须保持的合同

### 2.1 Adapter 与 Version

- Adapter 是逻辑管理对象。
- Adapter 创建后允许暂时没有 Version。
- AdapterVersion 是一次显式保存产生的不可变快照。
- **不存在 Draft 实体，也不增加 Draft 表。**
- Monaco 中尚未点击“保存新版本”的内容只存在于浏览器内存，不属于服务端持久化状态。
- 页面刷新或离开页面可能丢失未保存内容，这是 M1 可接受行为；前端应在存在未保存修改时提供最小提示，避免误操作。

### 2.2 Save 语义

“保存”在 M1 中必须明确表现为 **Save new version / 保存新版本**：

- 每次显式保存都新建一条 AdapterVersion。
- 已存在的 AdapterVersion 不允许修改。
- 保存成功后，新 Version 成为 `latest_version_id`。
- 保存 Version 同时保存：`code`、`requirements`、`runtime_config`。
- Adapter 的 `name` / `description` 属于元数据，单独更新，不因为修改描述而创建 Version。
- M1 不做自动保存；Monaco 每次输入不能产生 Version。

### 2.3 Publish 语义

- 发布只更新 `Adapter.published_version_id`。
- Publish **不创建新 Version**。
- Publish **不修改 Version 内容**。
- Publish **不改变 latest_version_id**。
- 允许发布旧版本，因此天然支持把稳定指针切回历史版本。
- 对已经发布的同一 Version 再次 Publish 应为幂等操作并返回成功。

### 2.4 M1 不执行代码

M1 中：

- 不 import Adapter 代码。
- 不调用 `handle(context, input)`。
- 不创建 Execution。
- Worker 不领取 Adapter 任务。
- requirements 只作为文本保存，不解析、不安装、不创建 venv。
- 不做 Python 语法校验作为保存前置条件；允许用户把尚未可运行的代码保存为一个版本。

真正的执行、依赖安装与 Execution 状态机全部留到 M2。

---

## 3. 数据模型

M1 新增两张表：`adapters`、`adapter_versions`。

### 3.1 adapters

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | BIGINT identity PK | Adapter ID |
| `name` | VARCHAR(128), NOT NULL, UNIQUE | 展示名称；API 输入需 trim，不能为空 |
| `description` | TEXT, NOT NULL, default `''` | 描述 |
| `language` | VARCHAR(16), NOT NULL | M1 API 仅允许 `python` |
| `latest_version_id` | BIGINT NULL FK → adapter_versions.id | 最近一次保存版本 |
| `published_version_id` | BIGINT NULL FK → adapter_versions.id | 当前发布版本 |
| `created_at` | TIMESTAMPTZ NOT NULL | UTC 创建时间 |
| `updated_at` | TIMESTAMPTZ NOT NULL | UTC 最近状态变化时间 |

说明：

- `latest_version_id` / `published_version_id` 初始均为 NULL。
- M1 不增加 status、draft、deleted_at 等字段。
- `language` 保留字段是为了未来多语言，但 M1 只接受 `python`；不要为语言建立复杂枚举框架。
- Adapter 元数据修改、保存新版本、发布版本时都应更新 `updated_at`。

### 3.2 adapter_versions

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | BIGINT identity PK | Version ID |
| `adapter_id` | BIGINT NOT NULL FK → adapters.id ON DELETE CASCADE | 所属 Adapter |
| `seq` | INTEGER NOT NULL, CHECK > 0 | Adapter 内版本号：1,2,3... |
| `code` | TEXT NOT NULL | Python 源码 |
| `requirements` | TEXT NOT NULL, default `''` | pip-style requirements 文本，M1 不解析 |
| `runtime_config` | JSONB NOT NULL, default `{}` | 非敏感配置，必须为 JSON object |
| `created_at` | TIMESTAMPTZ NOT NULL | UTC 创建时间 |

数据库约束：

- `UNIQUE(adapter_id, seq)`。
- Version 不提供 UPDATE / DELETE API。
- `runtime_config` 不允许保存真实 Secret；Secret 仍不进入数据库。

### 3.3 指针完整性

`adapters.latest_version_id` 和 `published_version_id` 必须只由领域服务修改，公共 API 不允许直接设置这两个字段。

服务必须保证指针指向**同一个 Adapter 的 Version**。不要提供通用“修改 version pointer”接口。

Schema 中可以建立真实 FK；由于 Adapter 与 AdapterVersion 存在双向引用，Alembic migration 应按清晰顺序创建表并在 Version 表建立后再添加两个 pointer FK，避免依赖创建顺序问题。

删除 Adapter 时，应确保 pointer 不造成删除顺序问题；实现可在同一事务内先清空 latest/published pointer，再删除 Adapter，由 `adapter_versions.adapter_id ON DELETE CASCADE` 清理 Version。

---

## 4. Version 并发与事务合同

“保存新版本”必须是一个数据库事务：

1. 读取目标 Adapter 并对该 Adapter 行加 `SELECT ... FOR UPDATE` 锁。
2. 在锁内计算该 Adapter 当前最大 `seq`，新版本 `seq = max + 1`；没有版本则为 1。
3. 插入新的 AdapterVersion。
4. 更新 `Adapter.latest_version_id` 为新 Version。
5. 更新 Adapter `updated_at`。
6. 一次提交。

目的：同一个 Adapter 同时收到两个 Save 请求时，不能产生重复 `seq`，也不能让 latest 指向错误版本。

不同 Adapter 的保存不应互相锁住。

Publish 同样必须在单事务内完成，并验证 Version 属于当前 Adapter。

---

## 5. Control API

API 继续使用 `/api` 前缀。

### 5.1 Adapter CRUD

#### `GET /api/adapters`

返回 Adapter 列表，M1 可直接返回全部记录，按 `updated_at DESC, id DESC` 排序；本阶段不为小规模单管理员场景提前增加分页框架。

返回字段：

```json
[
  {
    "id": 1,
    "name": "example-adapter",
    "description": "",
    "language": "python",
    "latest_version_id": 3,
    "published_version_id": 2,
    "created_at": "...",
    "updated_at": "..."
  }
]
```

#### `POST /api/adapters`

请求：

```json
{
  "name": "example-adapter",
  "description": "optional",
  "language": "python"
}
```

行为：

- 创建 Adapter，但不自动创建 Version。
- `language` 缺省时可默认 `python`；传入其他语言返回 422。
- 成功返回 201。
- `name` trim 后长度 1–128。
- name 冲突返回 409，稳定错误码 `adapter_name_conflict`。

#### `GET /api/adapters/{adapter_id}`

存在返回 200；不存在返回 404 / `adapter_not_found`。

#### `PATCH /api/adapters/{adapter_id}`

M1 只允许修改：

- `name`
- `description`

不允许客户端修改：

- id
- language
- latest_version_id
- published_version_id
- created_at / updated_at

name 冲突返回 409。

#### `DELETE /api/adapters/{adapter_id}`

- M1 删除 Adapter 及其所有 Version。
- 成功返回 204。
- 不存在返回 404。
- M2 引入 Execution 后，必须重新评估“已有执行历史的 Adapter 是否允许物理删除”；M1 不提前引入 soft-delete。

### 5.2 Version API

#### `GET /api/adapters/{adapter_id}/versions`

- 返回该 Adapter 的版本摘要。
- 按 `seq DESC` 排序。
- 至少返回：id、adapter_id、seq、created_at。

#### `GET /api/adapters/{adapter_id}/versions/{version_id}`

返回完整 Version：

- id
- adapter_id
- seq
- code
- requirements
- runtime_config
- created_at

Version ID 存在但不属于该 Adapter 时，不泄露跨 Adapter 信息，按 `version_not_found` 返回 404。

#### `POST /api/adapters/{adapter_id}/versions`

语义：**Save new version**。

请求：

```json
{
  "code": "def handle(context, input):\n    return input\n",
  "requirements": "",
  "runtime_config": {}
}
```

校验：

- `code` 必须是 string，且不能为空白字符串。
- `requirements` 必须是 string，可为空。
- `runtime_config` 必须是 JSON object，可为空对象；数组、标量、null 不接受。
- M1 不做 Python AST/compile 校验。
- M1 不解析 requirements。

成功返回 201，新 Version 自动成为 latest。

#### `POST /api/adapters/{adapter_id}/versions/{version_id}/publish`

行为：

- 验证 Adapter 与 Version 存在且归属一致。
- 设置 `published_version_id = version_id`。
- 不修改 `latest_version_id`。
- 已经发布该 Version 时返回 200，保持幂等。
- 可以发布历史 Version。

### 5.3 Domain Error 格式

不引入复杂异常框架。

领域错误统一采用 FastAPI `HTTPException` 的 detail object：

```json
{
  "detail": {
    "code": "adapter_not_found",
    "message": "Adapter not found"
  }
}
```

M1 至少稳定支持：

- `adapter_not_found` → 404
- `version_not_found` → 404
- `adapter_name_conflict` → 409

Pydantic/FastAPI 请求结构校验继续使用默认 422，不额外包装。

---

## 6. Backend 代码组织

在现有 M0 结构上增量扩展，建议：

```text
backend/src/dlr/control/
├── api/
│   ├── health.py
│   └── adapters.py
├── models/
│   ├── __init__.py
│   └── adapter.py
├── schemas/
│   ├── __init__.py
│   └── adapter.py
├── services/
│   ├── __init__.py
│   └── adapter.py
├── app.py
└── db.py
```

职责：

- `models`：SQLAlchemy persistence model。
- `schemas`：Pydantic request / response。
- `services`：事务、版本号、publish、领域规则。
- `api`：HTTP 参数/状态码/调用 service，不堆积领域逻辑。
- `db.py`：在现有 engine 基础上增加 SQLAlchemy 2 Session factory / dependency / declarative Base。

不要引入 repository pattern、unit-of-work framework、CQRS、event bus 等额外抽象。

Alembic `target_metadata` 在 M1 接入实际 SQLAlchemy metadata，并新增第一条真实 migration。

---

## 7. Web UI 详细设计

M1 使用 React + TypeScript + Vite，继续不引入 Redux/Zustand/复杂 UI 框架。

新增依赖只允许 Monaco 所必需的依赖，例如 `@monaco-editor/react` / `monaco-editor` 的稳定兼容版本。

### 7.1 页面结构

M1 只做一个轻量管理页，不引入路由框架也可以完成：

```text
┌─────────────────────────────────────────────────────────────┐
│ DataLinkRuntime                                             │
├──────────────────┬──────────────────────────────────────────┤
│ Adapter List     │ Selected Adapter                         │
│                  │                                          │
│ + New Adapter    │ Metadata: name / description             │
│ adapter-a        │ [Update details]                         │
│ adapter-b        │                                          │
│                  │ Version: v3  Latest / Published badges   │
│                  │ [version selector] [Publish]              │
│                  │                                          │
│                  │ Monaco Python Editor                     │
│                  │                                          │
│                  │ Requirements textarea                    │
│                  │ Runtime Config JSON textarea             │
│                  │                                          │
│                  │ [Save new version]                       │
└──────────────────┴──────────────────────────────────────────┘
```

视觉只需要清晰可用；不在 M1 做设计系统。

### 7.2 新建 Adapter

新建只要求：

- name
- description（可空）

language 固定 Python。

创建成功后自动选中新 Adapter。

如果 Adapter 尚无 Version，编辑器初始化一个浏览器端 starter：

```python
def handle(context, input):
    return input
```

这段内容**在用户点击 Save new version 前不能写入数据库**。

### 7.3 编辑历史 Version

- 默认载入 latest Version。
- Version selector 可以切换到任一历史版本并加载其 code / requirements / runtime_config。
- 历史 Version 永远只读于服务端；用户在 Monaco 中基于历史 Version 修改后点击 Save，产生一个**新的** Version，不更新旧 Version。
- 当前 Version 应显示 seq，并显示 Latest / Published 标记。

### 7.4 Requirements

M1 使用普通 textarea 即可：

```text
requests==...
tencentcloud-sdk-python==...
```

M1 不做安装结果、依赖冲突、包搜索等功能。

### 7.5 Runtime Config

M1 使用 JSON 文本编辑区即可，不额外引入 JSON Form 框架。

前端保存前：

- JSON 必须可解析。
- 顶层必须为 object。
- 无效时在本地显示错误并禁止 Save。

后端仍必须做同样的结构校验，不能依赖前端。

### 7.6 Dirty state

当 code / requirements / runtime_config 与当前加载版本不同，标记 `Unsaved changes`。

至少在以下操作发生前给出最小确认：

- 切换 Adapter；
- 切换 Version；
- 删除当前 Adapter。

不需要实现服务端 Draft 或自动恢复。

### 7.7 Adapter metadata

name / description 与 Version 内容分开保存：

- `Update details` → PATCH Adapter。
- `Save new version` → POST Version。

避免一个模糊的 Save 按钮同时改变元数据和版本。

### 7.8 Delete

删除 Adapter 必须有确认。

M1 无 Execution，因此确认后允许物理删除 Adapter + Versions。

---

## 8. M1 暂不实现认证的阶段边界

v1 总体架构仍要求 `DLR_ADMIN_TOKEN` 和 `DLR_WORKER_TOKEN`。

M1 暂不增加认证 UI / Token 流程，原因是：

- M1 仍无任何用户代码执行入口；
- 本阶段保持管理功能实现聚焦；
- 避免为 M1 临时增加一个随后又要调整的浏览器 Token UX。

这只是实施顺序，不改变 v1 安全合同。

**M2 的阻塞条件：在任何 Manual execution / Worker task API 暴露之前，必须先实现对应 Admin Token / Worker Token 鉴权。**

M1 仍然禁止数据库或 Git 中出现真实 Secret。

---

## 9. 测试与 CI

### 9.1 Backend tests

至少覆盖：

- 创建 Adapter 成功。
- name 为空/非法 language 校验。
- duplicate name → 409。
- Adapter GET / PATCH / DELETE。
- Adapter 不存在 → 404。
- 首次 Save 得到 seq=1。
- 连续 Save 得到递增 seq，并更新 latest。
- Version 内容保存后不可通过 API 修改。
- runtime_config 非 object → 422。
- Version 不属于 Adapter → 404。
- Publish 更新 published，不改变 latest。
- Publish 历史 Version 成功。
- 重复 Publish 同一 Version 幂等。

不要使用 SQLite 来代替 PostgreSQL 验证 PostgreSQL 特有的 migration / JSONB / FK 行为。

### 9.2 Real PostgreSQL smoke

在现有 `compose-smoke` 基础上扩展 M1 真实链路，至少验证：

```text
Alembic upgrade head
→ POST Adapter
→ GET Adapter
→ PATCH Adapter
→ Save Version #1
→ Save Version #2
→ 验证 latest = #2
→ Publish Version #1
→ 验证 published = #1 且 latest 仍 = #2
→ GET version list/detail
→ DELETE Adapter
→ 再 GET 返回 404
```

继续使用 smoke 自己的隔离 Compose Project 与独立数据库卷。

不要为了 JSON 提取重新引入 jq；可使用现有容器内 Python 标准库或其他不增加宿主依赖的简单方式。

### 9.3 Frontend tests

至少覆盖：

- Adapter list 加载成功。
- 创建 Adapter 后选中。
- 无 Version 时出现 starter code。
- runtime_config 非法 JSON 时阻止保存。
- Save new version 请求内容正确。
- 切换 Version 加载对应 snapshot。
- Publish 后 Published 状态更新。
- contradictory / failed API response 显示错误，不伪装成功。

Monaco 本身不需要测试其第三方编辑能力；测试 DLR 对编辑器 value / change / Save 的业务集成即可。

### 9.4 Existing quality gates

M1 必须继续通过：

- Ruff check
- Ruff format --check
- mypy strict
- pytest
- ESLint
- TypeScript typecheck
- Vitest
- Vite build
- Alembic on PostgreSQL
- compose-smoke
- GitHub Actions 全绿

---

## 10. 实施任务拆分

建议 Qoder 按以下顺序实施，但允许在不改变合同的前提下调整普通实现细节。

### Task 1 — Persistence foundation

- 扩展 `db.py`：Base、Session factory、FastAPI session dependency。
- 新增 Adapter / AdapterVersion models。
- Alembic target_metadata 接入。
- 新增 M1 migration。
- 验证 upgrade / downgrade / upgrade。

### Task 2 — Adapter CRUD API

- schemas。
- service。
- Adapter CRUD routes。
- domain error codes。
- tests。

### Task 3 — Version + Publish API

- Save-new-version transaction。
- row lock + seq 递增。
- version list/detail。
- publish historical/latest version。
- immutable contract tests。

### Task 4 — Web Adapter management

- Adapter list / create / update / delete。
- Monaco。
- Version selector。
- requirements / runtime_config。
- Save new version / Publish。
- dirty-state 最小保护。
- frontend tests。

### Task 5 — Integration / CI

- 扩展 compose smoke 为真实 M1 CRUD/version/publish 链路。
- 确认 migration 在真实 PostgreSQL 16 上执行。
- 完整 lint/type/test/build。
- GitHub Actions。

---

## 11. M1 验收标准

M1 只有同时满足以下条件才算完成：

1. 可以创建 Adapter，创建后允许 0 个 Version。
2. 可以更新 name / description。
3. Monaco 可以编辑 Python code。
4. requirements 与 runtime_config 可以编辑。
5. 每次显式 Save 必须创建新的不可变 Version。
6. Version seq 从 1 单调递增，同 Adapter 并发 Save 不产生重复 seq。
7. latest 永远指向最近保存 Version。
8. 可以查看版本列表与任一 Version snapshot。
9. 可以发布任一历史/最新 Version。
10. Publish 不改变 latest、不修改 Version。
11. 删除 Adapter 会删除 M1 阶段的 Version 数据。
12. Control / Web / Postgres / Worker 四服务继续健康。
13. M1 不执行用户 Adapter 代码、不创建 Execution、不安装 requirements。
14. Alembic + PostgreSQL 真实迁移通过。
15. Backend / Frontend / compose smoke / GitHub Actions 全绿。
16. PR 经独立 Code Review 通过后才能 merge。

---

## 12. 明确禁止的范围扩张

M1 不允许顺手实现：

- Execution 表或执行 API；
- Worker registration / heartbeat / polling；
- Runtime `handle()` harness；
- version-scoped venv；
- pip install；
- Secret Store / secret DB；
- Admin/Worker Token 实现；
- Schedule / Webhook；
- SSE 实时日志；
- AI；
- JavaScript / Java；
- Draft 表；
- AdapterInstance；
- RBAC；
- Repository / UnitOfWork / CQRS / EventBus 等额外架构层；
- 前端状态管理框架或设计系统。

如果实现过程中发现必须修改核心数据模型、公共 API、Runtime Contract、安全边界或部署架构，应停止并提出具体问题，不得自行改合同。

---

## 13. PR 要求

实现分支：

```text
feat/m1-adapter-management
```

PR 目标：

```text
feat/m1-adapter-management → main
```

PR 必须说明：

- 数据库 migration；
- API 清单；
- UI 功能；
- 所有测试与真实结果；
- compose smoke 结果；
- GitHub Actions 结果；
- 已知限制；
- 明确确认没有开始 M2。

不得自动 merge。完成后等待独立 Code Review。
