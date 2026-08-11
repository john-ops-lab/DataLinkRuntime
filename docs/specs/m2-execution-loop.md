# M2 执行闭环——详细设计

> 状态：批准实施  
> 基线：M1 已合并到 `main`  
> 实施分支：`feat/m2-execution-loop`  
> 范围：管理员/Worker Token、Worker 注册与心跳、Manual Execution、任务领取、version-scoped venv、Python 子进程 Runtime、Execution 持久化、大字段策略  
> 非范围：执行历史页面、实时日志 SSE、Schedule、Webhook、AI、JavaScript/Java、RBAC、任务重试、优先级队列、代码沙箱

## 1. M2 目标

M2 要打通 DLR 第一次真正的运行闭环：

```text
已保存 AdapterVersion
→ 创建 Manual Execution
→ Worker 主动领取任务
→ 准备该 Version 独立 venv
→ 全新子进程执行 Python Adapter
→ 收集 Output / stdout / stderr
→ 回写 Execution
→ 查询最终状态与结果
```

M2 完成后，Control 仍然不运行用户代码；所有 Adapter 代码只在 Worker 子进程中执行。

M2 不做完整运行体验页面。测试输入面板、Output 展示、实时日志与执行历史属于 M3；M2 通过 API、自动化测试和真实 Compose smoke 验证闭环。

---

## 2. 本阶段必须保持的核心合同

### 2.1 Version 绑定

- 每个 Execution 创建时必须明确绑定一个不可变 `version_id`。
- Manual Execution 默认执行 Adapter 的 `latest_version_id`。
- Manual API 允许显式指定该 Adapter 的任一历史 Version，便于回归测试。
- Execution 一旦创建，后续 Save / Publish 都不能改变该 Execution 的 `version_id`。
- 已经开始的 Execution 不受后续 Publish 影响。

### 2.2 执行边界

- Control 只负责创建任务、持久化、任务领取与结果接收。
- Worker 负责依赖环境、子进程、超时、日志与结果采集。
- Control 绝不 import、compile 或执行 Adapter 代码。
- 每次 Execution 必须启动一个全新子进程。
- venv 与子进程是运行/依赖隔离，不是安全沙箱。
- v1 继续采用 trusted-code model，不引入容器级按任务隔离、seccomp、Firecracker 等方案。

### 2.3 不做任务重试

M2 不实现业务任务自动重试、优先级、重新入队或 lease/requeue。

状态主链路：

```text
pending → running → succeeded
                  → failed
                  → timeout
```

`cancelled` 作为长期状态值保留，但 M2 不提供取消 API。

若 Worker 在领取任务后异常退出，Execution 可能暂时停留在 `running`；自动恢复属于后续可靠性增强，不在 M2 为此引入任务租约系统。

---

## 3. 认证与安全

M2 在暴露任何执行/Worker 任务 API 前，必须实现此前已经确认的两类静态 Token。

### 3.1 管理员 Token

配置：

```text
DLR_ADMIN_TOKEN
```

协议：

```http
Authorization: Bearer <DLR_ADMIN_TOKEN>
```

要求：

- `/api/health` 保持无需认证。
- Adapter / Version / Publish / Execution 等管理员 API 统一要求 Admin Token。
- 新增一个最小校验入口，例如 `GET /api/auth/admin/verify`，用于 Web 验证 Token。
- 服务端使用常量时间比较，例如 `secrets.compare_digest`。
- Token 不进入数据库、不进入 Git、不打印到日志。
- 未配置服务端 Token 时，受保护 API 返回 503；错误 Token 返回 401。

Web 只做最小 Token UX：

- 无有效 Token 时显示一个简单的管理员 Token 输入界面。
- 校验成功后存入浏览器 `sessionStorage`，不写 `localStorage`，不进入数据库。
- API client 自动添加 `Authorization: Bearer ...`。
- 收到 401 时清理当前浏览器会话 Token 并回到输入界面。
- 不新增 User / Role / Permission / Session 表。

这是单管理员共享 Token，不代表完整多用户身份体系；多个浏览器仍可并发使用平台。

### 3.2 Worker Token

配置：

```text
DLR_WORKER_TOKEN
```

Worker 所有内部 API 使用同样的 Bearer Header，但服务端校验的是 Worker Token。

- Worker 表不保存 token/token_hash。
- M2 仍使用一个平台级共享 Worker Token。
- 每 Worker 独立凭据留待未来确有需求时再设计。

### 3.3 Adapter 子进程环境

Worker 启动 Adapter 子进程时，不主动传入：

- `DLR_WORKER_TOKEN`
- `DLR_ADMIN_TOKEN`
- `DATABASE_URL`
- Control 内部配置

Adapter Runtime 只需要继承必要的基础环境以及 `DLR_SECRET_*`。

这只是减少无必要的凭据暴露，不构成安全沙箱；trusted-code 边界不变。

---

## 4. 数据模型

M2 新增两张表：`workers`、`executions`。

### 4.1 workers

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | BIGINT identity PK | Worker ID |
| `name` | VARCHAR(128), NOT NULL, UNIQUE | 稳定 Worker 名称 |
| `status` | VARCHAR(16), NOT NULL | `online / offline` |
| `last_heartbeat` | TIMESTAMPTZ NOT NULL | 最近心跳 |
| `capabilities` | JSONB NOT NULL | M2 固定包含 `python` |
| `created_at` | TIMESTAMPTZ NOT NULL | 创建时间 |
| `updated_at` | TIMESTAMPTZ NOT NULL | 更新时间 |

Worker 重启后使用相同 `name` 注册时复用原记录并更新状态，不重复创建。

M2 不实现独立 Worker 凭据、不保存 Token、不做 Worker 删除 API。

### 4.2 executions

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `id` | BIGINT identity PK | Execution ID |
| `adapter_id` | BIGINT NOT NULL FK | 所属 Adapter |
| `version_id` | BIGINT NOT NULL FK | 本次执行固定版本 |
| `worker_id` | BIGINT NULL FK | 领取任务后设置 |
| `trigger` | VARCHAR(16), NOT NULL | M2 仅 `manual` |
| `status` | VARCHAR(16), NOT NULL | `pending/running/succeeded/failed/timeout/cancelled` |
| `input` | JSONB NOT NULL | 输入，允许 JSON null |
| `output` | JSONB NULL | 未超限的完整 Output |
| `output_size` | BIGINT NULL | Output UTF-8 JSON 字节数 |
| `output_truncated` | BOOLEAN NOT NULL | Output 是否因超限未完整保存 |
| `output_preview` | TEXT NULL | 超限时的人类可读文本预览 |
| `stdout` | TEXT NOT NULL | 截断后的 stdout |
| `stdout_truncated` | BOOLEAN NOT NULL | stdout 是否截断 |
| `stderr` | TEXT NOT NULL | 截断后的 stderr |
| `stderr_truncated` | BOOLEAN NOT NULL | stderr 是否截断 |
| `error` | TEXT NULL | 失败/超时摘要 |
| `created_at` | TIMESTAMPTZ NOT NULL | 入队时间 |
| `started_at` | TIMESTAMPTZ NULL | Worker 领取时间 |
| `ended_at` | TIMESTAMPTZ NULL | Control 收到终态结果时间 |
| `duration_ms` | BIGINT NULL | Control 基于 started/end 计算 |

约束：

- `adapter_id` / `version_id` 使用限制删除的真实 FK，Execution 历史不能因删除 Adapter/Version 而失去关联。
- `worker_id` 可为空，保留任务未领取语义。
- 对 `status`、`trigger` 建简单 CHECK 约束即可，不引入复杂状态机框架。
- 建立任务领取所需索引，例如 `(status, created_at, id)`。

### 4.3 M2 后的 Adapter 删除语义

M1 阶段没有 Execution，因此允许物理删除 Adapter + Versions。

M2 开始后：

- Adapter **没有任何 Execution**：仍允许原来的物理删除。
- Adapter **已经存在 Execution**：`DELETE` 返回 409，稳定错误码 `adapter_has_executions`。
- M2 不引入 soft-delete；这是保留执行历史完整性的最小方案。

---

## 5. Execution 大字段策略

默认限制统一放到配置中：

```text
input                 512 KiB
output                512 KiB
output_preview         16 KiB
stdout                 1 MiB
stderr                 1 MiB
```

字节大小按 UTF-8 计算。

### 5.1 Input

创建 Execution 前，Control 对紧凑 JSON 序列化后的 UTF-8 字节数进行检查。

- `<= 512 KiB`：允许创建。
- `> 512 KiB`：返回 413 / `execution_input_too_large`。
- 超限时不创建 Execution，绝不截断 Input 后执行。

### 5.2 Output

Worker 的 harness 先确保 Adapter 返回值能完整 JSON 序列化，并写入输出文件。

- `<= 512 KiB`：解析完整 JSON，写入 `output`。
- `> 512 KiB`：不上传完整 JSON，不产生残缺 JSON；设置 `output=null`、`output_truncated=true`、记录 `output_size` 和最多 16 KiB 的 `output_preview` 文本。

Output 超限不代表 Adapter 执行失败；Execution 仍可为 `succeeded`。

### 5.3 stdout / stderr

为避免用户代码大量日志耗尽 Worker 内存：

- 子进程 stdout / stderr 先写临时文件，不使用无限制内存 `PIPE` 累积。
- 执行结束后读取并按 1 MiB 上限截断。
- 截断时优先保留日志头部和尾部，中间插入明确的截断标记，保证 traceback 尾部仍可见。
- 设置对应 `*_truncated=true`。

Worker 向 Control 上报前，对 stdout / stderr / error 做一次基于 `DLR_SECRET_*` 当前明文值的精确替换脱敏；这只是 best-effort，不视为安全沙箱。

---

## 6. 管理员 Execution API

### 6.1 创建 Manual Execution

```http
POST /api/adapters/{adapter_id}/executions
Authorization: Bearer <admin-token>
```

请求：

```json
{
  "input": {"example": true},
  "version_id": null
}
```

语义：

- `version_id` 省略/null：使用当前 `latest_version_id`。
- 显式指定：允许运行该 Adapter 的任一历史 Version。
- Adapter 不存在：404 / `adapter_not_found`。
- Adapter 没有任何 Version：409 / `adapter_has_no_version`。
- 显式 Version 不属于该 Adapter：404 / `version_not_found`。
- Input 超限：413 / `execution_input_too_large`，不落库。
- 成功创建：202，状态 `pending`。

### 6.2 查询 Execution

```http
GET /api/executions/{execution_id}
Authorization: Bearer <admin-token>
```

返回 Execution 当前完整状态与已落库结果。

M2 不实现 Execution 列表/历史页面；M3 再增加列表和 UI。

---

## 7. Worker 内部 API

Worker API 使用 Worker Token，不通过 Web UI。

### 7.1 注册

```http
POST /api/workers/register
```

请求：

```json
{
  "name": "worker-1",
  "capabilities": ["python"]
}
```

行为：按 `name` upsert，设置 `online` 与当前 `last_heartbeat`，返回 Worker ID。

### 7.2 心跳

```http
POST /api/workers/{worker_id}/heartbeat
```

- 更新 `last_heartbeat`。
- 设置 `status=online`。
- 204。

Worker 默认每 10 秒心跳一次。

### 7.3 优雅离线

```http
POST /api/workers/{worker_id}/offline
```

Worker 收到正常退出信号时 best-effort 调用，设置 `status=offline`。

异常崩溃后的自动离线判定不在 M2 引入调度器；后续可基于 heartbeat 超时计算。

### 7.4 长轮询领取任务

```http
POST /api/workers/{worker_id}/tasks/claim?wait_seconds=20
```

- 默认等待 20 秒，最大 30 秒。
- 有任务返回 200 + Task Payload。
- 到期无任务返回 204。

单次领取必须使用 PostgreSQL：

```text
SELECT pending Execution
ORDER BY created_at, id
FOR UPDATE SKIP LOCKED
LIMIT 1
```

然后在同一个事务内：

```text
status = running
worker_id = 当前 Worker
started_at = 数据库当前时间
```

这样多个 Worker 同时领取任务时，同一个 Execution 只能被领取一次；不同任务可并发领取。

Task Payload 至少包含：

- execution_id
- adapter_id
- version_id
- code
- requirements
- runtime_config
- input
- latest_version_id
- published_version_id
- execution_timeout_seconds

Version 内容来自不可变 AdapterVersion 快照，不从浏览器 Working Copy 获取。

### 7.5 回报执行结果

```http
POST /api/workers/{worker_id}/executions/{execution_id}/result
```

要求：

- Execution 必须已经分配给当前 Worker。
- 只接受 `succeeded / failed / timeout`。
- Control 再次校验大字段合同，不能完全信任客户端。
- Control 写入结果后设置 `ended_at`，并用服务端时间计算 `duration_ms`。
- 对已经处于终态的同一 Execution 再次上报，按幂等成功处理，支持“服务端已写入但响应丢失”的网络重试。

Worker 对结果回报可做有限的 HTTP 传输重试；这不属于业务任务自动重跑。

---

## 8. Worker Agent

配置建议：

```text
DLR_CONTROL_URL=http://control:8000
DLR_WORKER_TOKEN=...
DLR_WORKER_NAME=worker-1
DLR_WORKER_HEARTBEAT_SECONDS=10
DLR_WORKER_CLAIM_WAIT_SECONDS=20
DLR_WORKER_MAX_CONCURRENCY=4
DLR_RUNTIME_ROOT=/var/lib/dlr/runtime
DLR_EXECUTION_TIMEOUT_SECONDS=300
DLR_DEP_INSTALL_TIMEOUT_SECONDS=300
DLR_PYPI_INDEX_URL=        # 可选
```

Worker 启动流程：

```text
读取配置
→ 注册/复用 Worker
→ 启动心跳
→ 按空闲并发槽长轮询领取任务
→ 在线程池中执行任务
→ 回报终态结果
```

M2 默认最大并发 4，可配置。

Control 与 Worker 通信继续遵守：**Worker 主动外连，Worker 不开放入站 HTTP 端口。**

Control 暂时不可用时，Worker 应以简单有上限的退避继续注册/心跳/领取任务，不崩溃退出；不要引入复杂重试框架。

---

## 9. version-scoped venv

运行目录：

```text
{DLR_RUNTIME_ROOT}/
└── adapters/{adapter_id}/versions/{version_id}/
    ├── adapter.py
    ├── requirements.txt
    ├── .venv/
    └── .ready
```

规则：

1. 每个 AdapterVersion 一个独立 venv。
2. 第一次执行该 Version 时惰性创建。
3. Worker 镜像已经包含 `uv`，优先使用 `uv venv` + `uv pip install`，不新增依赖管理框架。
4. requirements 为空时仍创建独立 venv，但不执行额外安装。
5. 只有依赖准备完整成功后才写 `.ready`。
6. 检测到不完整目录但无 `.ready` 时删除后重建。
7. 同一 Worker 内同一 Version 并发首次执行时使用轻量进程内 lock，避免重复构建 venv。
8. 可通过 Worker 配置指定内部 PyPI/镜像源，不把凭据写进 Version 或日志。

### 9.1 venv 清理

保持架构中的最简磁盘策略：

- 必须保留当前 Adapter 的 `latest`、`published` 和正在执行中的 Version。
- 其他历史 Version 的 venv 可在任务完成后 best-effort 清理，需要再次执行时重新构建。
- 清理失败只记录 Worker 日志，不改变 Execution 成败。
- 不做跨 Adapter 依赖共享、全局 wheel cache 管理或镜像构建。

Worker runtime 目录通过独立 Docker named volume 持久化，避免 Worker 容器重建后所有 venv 必然丢失。

---

## 10. Python Runtime Contract

固定入口：

```python
def handle(context, input):
    ...
```

`context`：

```text
context.config
context.secrets.get(key)
context.logger
```

### 10.1 context.config

直接来自当前 AdapterVersion 的 `runtime_config`，只读语义。

### 10.2 context.secrets

`context.secrets.get(key)`：

- key 要求使用大写字母、数字和下划线，例如 `CLOUD_SECRET_KEY`。
- 从 Worker 环境变量 `DLR_SECRET_<KEY>` 读取。
- 找不到返回 `None`。
- 真实值不经过 Control、不进入数据库。

### 10.3 context.logger

使用 Python 标准 logging，输出到 stdout，由 Worker 捕获。

### 10.4 harness

harness 尽量只依赖 Python 标准库，使它可以直接由 Version venv 的 Python 执行。

执行路径：

```text
读取 input.json / runtime_config.json
→ import 当前 workspace 的 adapter.py
→ 构建 context
→ 调用 handle(context, input)
→ JSON 序列化返回值
→ 写 output.json
```

异常：打印 traceback 到 stderr，并以非 0 退出。

返回值不可 JSON 序列化：按执行失败处理。

---

## 11. 子进程、超时与日志

Worker 使用当前 Version venv 的 Python 启动 harness。

- 每个 Execution 全新进程。
- 默认 Adapter 执行超时 300 秒，可配置。
- 依赖安装超时与 Adapter 执行超时分开计算。
- POSIX 下使用独立 process group/session；超时时终止整个进程组，避免简单子进程残留。
- stdout/stderr 重定向到临时文件，执行完成后按 §5 截断读取。
- 依赖准备失败：Execution=`failed`，error 明确为依赖准备失败，并保留有限安装日志。
- Adapter 抛异常/非零退出：Execution=`failed`。
- 超时：Execution=`timeout`。
- 正常输出：Execution=`succeeded`，即使 Output 因大字段策略只保存 preview。

---

## 12. Docker Compose

继续保持四服务：

```text
web / control / postgres / worker
```

M2 不新增 Redis、MQ、调度器、日志组件。

新增：

- Control 注入 `DLR_ADMIN_TOKEN`、`DLR_WORKER_TOKEN`。
- Worker 注入 `DLR_WORKER_TOKEN`、`DLR_CONTROL_URL`、Worker 名称与执行参数。
- Worker 挂载独立 `dlr_worker_runtime` named volume。
- `DLR_SECRET_*` 只注入 Worker。
- 提供 `.env.example`，只放占位值，不放真实凭据。
- 默认 Compose 不硬编码可用于生产的 Token；README 说明本地启动方式。

---

## 13. 代码组织建议

在现有结构上最小增量扩展：

```text
backend/src/dlr/
├── common/
│   ├── config.py
│   └── ...共享状态/DTO
├── control/
│   ├── api/
│   │   ├── auth.py
│   │   ├── executions.py
│   │   └── workers.py
│   ├── models/
│   │   ├── adapter.py
│   │   └── execution.py
│   ├── schemas/
│   └── services/
├── runtime/
│   └── harness.py
└── worker/
    ├── agent.py
    ├── client.py
    ├── executor.py
    └── venv.py
```

允许按职责适度调整文件名，但不要引入 Repository / UnitOfWork / CQRS / EventBus / Agent Framework。

---

## 14. 测试要求

### 14.1 Control / PostgreSQL

至少覆盖：

- Admin Token 缺失/错误被拒绝，正确 Token 通过。
- Worker Token 与 Admin Token 不能互相替代。
- Worker 注册同名 upsert、心跳。
- 创建 Manual Execution 默认绑定 latest。
- 显式运行历史 Version。
- 无 Version → 409。
- 跨 Adapter Version → 404。
- Input 超 512 KiB → 413 且不创建 Execution。
- 两个独立 Worker/Session 并发 claim，单个 Execution 只能领取一次。
- pending → running 的 worker_id/start_time 正确。
- 非所属 Worker 不能上报结果。
- result 重复上报幂等。
- succeeded/failed/timeout 终态写入正确。
- Adapter 有 Execution 后 DELETE → 409 `adapter_has_executions`。
- 所有 PostgreSQL 特有行为继续使用真实 PostgreSQL，不用 SQLite 替代。

### 14.2 Worker / Runtime

至少覆盖：

- 空 requirements 可构建独立 venv。
- 同一 Version 的 ready venv 可复用。
- 不完整 venv 可重建。
- harness 正确传递 input / runtime_config。
- `context.secrets.get()` 可读取 `DLR_SECRET_*`。
- `context.logger` 被收集。
- 正常 JSON Output 成功。
- Adapter exception → failed。
- 非 JSON-serializable 返回值 → failed。
- timeout 能终止进程。
- Output 超限时只保留 size/preview，不保存残缺 JSON。
- stdout/stderr 大量输出不会无限累积到 Worker 内存，截断标记正确。
- 写操作上报前不会把 Worker/Admin Token 当作 Runtime Secret 传给 harness。

### 14.3 Web

至少覆盖：

- 未认证时显示 Admin Token 输入。
- 正确 Token 验证后进入现有 Adapter UI。
- API client 带 Bearer Header。
- 401 后清理会话 Token 并回到认证入口。
- M1 现有 Adapter/Version 功能回归测试保持通过。

### 14.4 真实 Compose smoke

在原 M1 smoke 基础上继续验证真实 M2 闭环：

```text
四服务 healthy
→ Alembic upgrade head
→ 未带 Admin Token 调用受保护 API，确认 401
→ 带 Admin Token 创建 Adapter
→ Save 一个 Python Version
→ 创建 Manual Execution
→ Worker 自动注册/心跳/领取
→ Worker 建立 version-scoped venv
→ 子进程执行
→ 轮询 Execution 到 succeeded
→ 验证 version_id 固定
→ 验证 input/runtime_config/output 正确
→ 验证一个 DLR_SECRET_* 可通过 context.secrets 使用，但真实 Secret 不出现在响应/日志
→ 验证 Worker runtime 目录存在对应 .venv/.ready
```

Smoke Adapter 只使用 Python 标准库，避免 CI 依赖公网 PyPI 可用性。

---

## 15. 验收标准

M2 只有同时满足以下条件才算完成：

1. Admin/Worker 两类 Token 已生效，真实 Token 不落库、不进 Git。
2. Worker 可注册、心跳并主动长轮询领取任务。
3. 多 Worker 领取使用 `FOR UPDATE SKIP LOCKED`，无重复领取。
4. Manual Execution 创建时固定绑定 Version。
5. Worker 每次在全新子进程中运行 Adapter。
6. 每个 AdapterVersion 使用独立可复用 venv。
7. requirements 在 Worker 侧安装，Control 不安装用户依赖。
8. Runtime Contract 的 config / secrets / logger 工作正确。
9. timeout 能终止 Adapter 进程并回写 `timeout`。
10. Execution 成功/失败结果正确落 PostgreSQL。
11. Input/Output/stdout/stderr 大字段策略全部有真实测试。
12. 有 Execution 的 Adapter 不再允许物理删除。
13. Control 仍不执行任何 Adapter 代码。
14. Worker 仍无入站端口，通信方向保持 Worker → Control。
15. M1 Adapter 管理功能不回归。
16. Backend / Web / Compose smoke / GitHub Actions 全绿。
17. PR 经独立 Code Review 通过后才能合并。

---

## 16. 明确禁止的范围扩张

M2 不允许顺手实现：

- Execution 历史页面；
- SSE 实时日志；
- Schedule / Webhook；
- AI；
- RAGFlow / 文件知识库；
- JavaScript / Java Runtime；
- AdapterInstance；
- 用户账号、RBAC、组织权限；
- 业务任务自动重试、优先级队列；
- Redis / RabbitMQ / Kafka；
- Kubernetes；
- 任务级容器编排或安全沙箱；
- 对象存储日志；
- Draft；
- Repository / UnitOfWork / CQRS / EventBus 等额外架构层。

若实现过程中确实必须修改已确认的核心数据模型、公共 API、Runtime Contract、安全边界、持久化方案或部署架构，必须停止并用中文提出具体决策问题，不得自行改变合同。

---

## 17. PR 要求

实施分支：

```text
feat/m2-execution-loop
```

PR 标题建议：

```text
feat: 实现 M2 执行闭环
```

PR 描述、验证结果、已知限制、Review 回复和完成总结必须使用中文。

完成后：

1. 检查最终 diff；
2. 推送 `feat/m2-execution-loop`；
3. 创建一个 PR 到 `main`；
4. 用中文报告修改内容和所有真实验证结果；
5. 明确披露失败、跳过项和已知限制；
6. 不合并；
7. 不开始 M3；
8. 等待独立 Code Review。
