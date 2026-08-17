# M3 可观测、执行体验与 Web UI 升级——详细设计

> 状态：批准实施  
> 基线：M2 已合并到 `main`  
> 实施分支：`feat/m3-observability-ux`  
> 目标：把 M2 已经跑通的执行能力变成可在浏览器中完整使用、观察和追溯的产品体验，同时完成现有 Web UI 的整体视觉升级。  
> 非范围：AI Editor、文件上传、RAGFlow、Schedule、Webhook、JavaScript/Java Runtime、用户账号/RBAC、Redis/Kafka/WebSocket、独立日志系统、新的数据库表。

## 1. M3 目标

M3 要形成第一阶段完整可用闭环：

```text
选择 Adapter / 已保存 Version
→ 输入测试 JSON
→ 运行测试
→ 看到 pending / running
→ 实时看到 stdout / stderr
→ 看到 succeeded / failed / timeout
→ 查看格式化 Output / Error
→ 从执行记录中重新查看本次 Execution
```

同时，M3 必须把当前工程验证型 Web 页面升级为统一、现代、清晰的企业级 Console 界面。

M3 完成后，DLR 应第一次具备“日常开发 Adapter、测试 Adapter、观察执行、回看历史”的完整产品形态。

---

## 2. 不变的核心合同

### 2.1 只运行已保存 Version

- Execution 必须继续绑定不可变 `version_id`。
- 测试运行显式绑定当前在 Web 中选中的已保存 Version，而不是偷偷改用 latest。
- Monaco Working Copy 存在未保存修改（dirty）时，禁止点击“运行测试”，提示“请先保存当前修改，再运行。”（M5.5.9 统一文案）。
- M3 不新增 Draft、临时 Version、临时代码执行通道。
- 历史 Version 可以直接测试；Execution 绑定对应历史 `version_id`。

### 2.2 最终结果仍由 M2 result 上报负责

M3 新增的实时日志只负责体验，不改变 M2 的最终正确性合同：

```text
实时 progress = best effort
最终 result = authoritative
```

- progress 丢失不触发业务重试。
- Execution 完成时，M2 已有 final result 仍会上报最终 stdout/stderr/output/status，并覆盖/校正运行中的临时进度状态。
- 不为实时日志引入消息队列、ACK、offset、日志重传协议。

### 2.3 不改变安全边界

- Admin API 继续使用 `DLR_ADMIN_TOKEN`。
- Worker API 继续使用 `DLR_WORKER_TOKEN`。
- 浏览器 Token 继续只存 `sessionStorage`。
- SSE Token 必须放在 `Authorization` Header，禁止放入 URL query。
- Runtime Secret 继续只存在 Worker 侧；实时日志 chunk 在 Worker 发送前必须复用 M2 的 Secret 脱敏逻辑。

---

## 3. Web 信息架构与视觉升级
## UI 视觉参考

以下图片作为 M3 的整体视觉与布局参考：

- 登录页：`docs/ui/m3/01-登录页.png`
- Adapter 编辑页：`docs/ui/m3/02-Adapter编辑页.png`
- 测试运行页：`docs/ui/m3/03-测试运行页.png`
- 执行记录页：`docs/ui/m3/04-执行记录页.png`

这些图片用于确定视觉风格、信息层级和页面布局，
不是逐像素实现要求。

实际功能、字段和交互以本规格文档为准。

### 3.1 总体视觉

M3 统一成现代企业级 Console 风格：

- 浅灰应用背景 + 白色主工作区。
- 高信息密度，但避免拥挤。
- 统一边距、字号、按钮、输入框、表格、状态 Badge、Tab、抽屉/弹层。
- 少量圆角和阴影，用于层级，不做花哨动效。
- 成功 / 运行中 / 失败 / 超时状态有清晰且一致的视觉语义。
- 所有面向人的 UI 文案使用中文。

允许引入一套成熟组件库（建议使用与当前 React 工具链兼容的稳定版 Ant Design）统一基础组件；不要自研 Design System。

仍然不引入：

- Redux / MobX 等全局状态框架；
- 微前端；
- 大型 CSS 架构；
- 因美化而重构后端或领域模型。

### 3.2 页面结构

保持单页工作台，不要求引入 Router。建议布局：

```text
顶部栏：DataLinkRuntime / Control 状态 / 当前会话状态
左侧栏：Adapter 搜索 / Adapter 列表 / 新建
主工作区：Adapter 名称 + Latest / Published + 操作
主工作区 Tab：编辑 | 测试运行 | 执行记录
```

### 3.3 登录页

替换当前简单输入框，做成正式登录卡片：

- 标题：DataLinkRuntime
- 说明：请输入管理员 Token
- 密码输入框
- 登录按钮
- 错误信息

认证合同仍完全沿用 M2，不新增账号体系。

### 3.4 编辑 Tab

保留 M1/M2 的全部能力，同时完成视觉重构：

- Adapter 元数据区域。
- Version selector。
- Latest / Published Badge。
- Monaco Editor。
- requirements。
- runtime_config。
- 保存新版本 / 发布 / 删除等操作。
- 原有 dirty-state、防串线、busy lock 行为不得回退。

---

## 4. 测试运行体验

### 4.1 测试运行 Tab

展示：

- 当前测试 Version（明确显示 `vN`、Latest/Published 状态）。
- Input JSON 编辑区。
- “运行测试”按钮。
- 当前 Execution 概览。
- Output / stdout / stderr 三个结果 Tab。

### 4.2 Input

Input 允许任意合法 JSON：

- object
- array
- string
- number
- boolean
- null

前端在创建 Execution 前仅做 JSON 解析校验；大小上限仍以 Control 的 M2 校验为最终准则。

Input 仅保存在当前浏览器工作区内，不需要新增数据库字段或独立模板系统。

### 4.3 运行行为

点击“运行测试”时：

1. 当前 Adapter 必须存在已保存 Version；
2. Working Copy 必须不 dirty；
3. Input 必须为合法 JSON；
4. 调用 `POST /api/adapters/{adapter_id}/executions`；
5. 请求中显式传当前选中的 `version_id`；
6. 202 后立即展示 Execution；
7. 建立 SSE 连接观察状态和日志；
8. 终态后加载/刷新完整 Execution 详情。

运行按钮在创建请求期间必须防重复提交。

---

## 5. 执行历史 API

新增管理员 API：

```http
GET /api/adapters/{adapter_id}/executions?limit=50&before_id=<execution_id>
Authorization: Bearer <admin-token>
```

默认：

- `limit=50`；
- 最大 `limit=100`；
- `ORDER BY id DESC`；
- 使用 `before_id` 游标，不使用 offset 分页。

列表只返回轻量摘要，不携带大字段 `input/output/stdout/stderr`。

建议摘要字段：

```json
{
  "id": 105,
  "adapter_id": 1,
  "version_id": 12,
  "version_seq": 5,
  "worker_id": 1,
  "worker_name": "worker-1",
  "trigger": "manual",
  "status": "succeeded",
  "created_at": "...",
  "started_at": "...",
  "ended_at": "...",
  "duration_ms": 2840
}
```

返回：

```json
{
  "items": [...],
  "next_before_id": 90
}
```

点击一条执行记录后继续复用 M2：

```http
GET /api/executions/{execution_id}
```

加载完整 Input / Output / stdout / stderr / error。

M3 不新增 Execution 表，不新增历史表。

---

## 6. Worker 实时进度上报

新增 Worker API：

```http
POST /api/workers/{worker_id}/executions/{execution_id}/progress
Authorization: Bearer <worker-token>
```

请求体保持极小：

```json
{
  "stdout_chunk": "...",
  "stderr_chunk": "..."
}
```

规则：

- 必须校验 Execution 属于当前 Worker。
- Execution 处于 `running` 时追加日志。
- Execution 已终态时允许直接 204 no-op，避免 progress 与 final result 尾部竞态影响最终结果；非所属 Worker仍返回 409。
- Control 持久化时继续遵守 stdout/stderr 1 MiB 上限和截断标记。
- progress 不修改 Output、error、ended_at、duration_ms。
- progress 不改变 Execution 状态。
- progress 不做业务重试。

### 6.1 Worker 发送策略

Worker 执行子进程时继续把 stdout/stderr 写入临时文件。

在子进程运行期间，约每 1 秒读取自上次 offset 后新增的字节，并调用 progress API。

- 没有新增内容就不发请求。
- progress 网络失败只记录 Worker 日志，继续执行，不影响 Execution。
- 不重跑 Adapter。
- chunk 在发送前必须经过 M2 已有 Secret 脱敏。
- 最终 result 仍上报完整最终状态与最终截断后的 stdout/stderr。

实现可在 `executor.run()` 中增加可选 progress callback，不要把 HTTP client 硬塞进 Runtime harness。

---

## 7. SSE 实时事件

新增管理员 SSE API：

```http
GET /api/executions/{execution_id}/events
Authorization: Bearer <admin-token>
Accept: text/event-stream
```

### 7.1 实现原则

M3 不引入 Redis Pub/Sub、Kafka、NATS、WebSocket Gateway。

Control 采用最简单的 PostgreSQL polling：

```text
SSE connection
→ 每约 0.5～1 秒重新读取 Execution
→ 状态或日志变化则发送事件
→ 终态发送最后事件并关闭
```

内部低并发 v1 场景可接受。

### 7.2 事件

至少支持：

```text
event: execution
```

用于状态快照/变化。

```text
event: log
```

用于 stdout/stderr 新增内容。

必要时可以在日志因截断而不再保持简单前缀关系时发送：

```text
event: log_snapshot
```

直接发送当前截断后的 stdout/stderr 快照。

Execution 到达 `succeeded / failed / timeout / cancelled` 后发送最终 execution 事件并关闭连接。

可每约 15 秒发送 SSE keepalive comment，避免代理空闲关闭。

### 7.3 Nginx

SSE 路径必须关闭代理缓冲，避免事件被 Nginx 累积后一次性返回。

仅对 SSE endpoint 做必要配置，不影响普通 API。

### 7.4 Web 客户端

浏览器不要使用原生 `EventSource`，因为管理员认证依赖自定义 Authorization Header。

使用 `fetch()`：

```ts
fetch(url, {
  headers: {
    Authorization: `Bearer ${token}`,
    Accept: "text/event-stream"
  }
})
```

读取 `response.body` stream 并解析 SSE。

禁止：

```text
/api/executions/105/events?token=xxx
```

Token 绝不进入 URL。

---

## 8. Output / 日志展示

### 8.1 Output

完整 Output：

- 对象/数组/标量统一以格式化 JSON 展示；
- 第一阶段直接使用 `JSON.stringify(value, null, 2)` 即可；
- 不要求引入大型 JSON Viewer。

`output_truncated=true`：

- 明确显示“Output 超过平台保存上限”；
- 展示 `output_size`；
- 展示 `output_preview`；
- 不把 preview 伪装成完整 JSON。

### 8.2 统一实时日志（M5.5.10 覆盖原 8.2）

M5.5.10 起，Workbench 与执行/调用详情不再有独立的 stdout / stderr 视图，改为一个
“实时日志”统一视图：

- 使用等宽字体和终端风格区域；
- stdout、stderr、`context.logger`、代码运行错误、Traceback、可捕获的第三方
  库输出与必要的平台 Runtime 状态消息按实际发生顺序在同一个流中展示；
- 每行统一时间前缀 `[YYYY-MM-DD HH:mm:ss]`，logger 行可附加 `[INFO]` /
  `[WARN]` / `[ERROR]` 级别标记；
- `*_truncated=true` 时显示明确提示；
- 默认自动滚动到最新内容；用户手动滚动或点击“暂停跟随”后停留在当前位置，
  新日志不得把用户强制拉回底部；点击“继续跟随”恢复。

> 实现要点：Worker 以 `stderr=subprocess.STDOUT` 启动适配器子进程，把两个流在
> 字节层面合并为实际顺序；读取时先复用 M3 的 Secret 脱敏逻辑，再对每一完整行
> 追加采集时间前缀（行缓冲保证跨切片的部分行不被打断）。实时日志 chunk 与最终
> 结果使用同一条 脱敏→行时间戳 流水线，因此 SSE 增量始终是最终文本的前缀。
> 合并流通过 stdout 通道上报（stderr 通道保持为空）；API 与 SSE 仍兼容旧的分流
> 上报，历史数据（旧 stdout/stderr 两列）在前端合并显示。

### 8.3 状态

统一中文显示：

- pending：等待中
- running：运行中
- succeeded：成功
- failed：失败
- timeout：超时
- cancelled：已取消（M3 仍无取消 API，只负责显示长期状态）

---

## 9. 执行记录 UI

“执行记录” Tab：

- 表格展示执行摘要；
- 默认最新在前；
- 状态 Badge；
- Version；
- Worker；
- Duration；
- 时间；
- “加载更多”使用 `before_id`。

点击一行使用右侧 Drawer 或主区详情面板展示：

- Execution ID；
- Adapter；
- Version；
- Worker；
- 状态；
- 时间/耗时；
- Input；
- Output；
- stdout；
- stderr；
- error。

不新增独立路由不是硬性要求，但默认优先保持当前单页工作台简单结构。

---

## 10. Worker 状态展示

为了顶部状态区/后续运维观察，M3 可以新增最小管理员只读 API：

```http
GET /api/workers
Authorization: Bearer <admin-token>
```

返回 Worker 的 id/name/status/last_heartbeat/capabilities。

注意：M2 没有“心跳超时自动改 offline”的调度器，因此 UI 不得仅凭数据库 `status=online` 宣称 Worker 永远在线。应同时展示最近心跳时间；如需前端提示“可能离线”，可按当前时间与 last_heartbeat 做纯展示层判断，但不要在 M3 引入后台调度任务修改状态。

---

## 11. 数据库与架构约束

M3 原则上 **不新增数据库表，不新增 Alembic migration**。

复用已有：

- executions
- workers
- adapters
- adapter_versions

如果实现者认为必须修改核心表结构或引入新基础设施，必须停止并说明必要性，不能自行扩大范围。

---

## 12. 必须测试

### 12.1 Backend

至少覆盖：

- Execution 历史按 Adapter 隔离；
- `before_id` 游标顺序/分页；
- 列表不返回大字段；
- progress 仅 owning Worker 可写；
- progress 追加 stdout/stderr；
- progress 超限仍遵守 1 MiB + truncated；
- terminal 后 progress no-op，最终 result 不被覆盖；
- SSE 需要 Admin Token；
- SSE 首次状态事件；
- progress 后 SSE 可观察日志变化；
- 终态后 SSE 发送最终状态并结束；
- worker 列表 Admin API 鉴权与返回。

### 12.2 Worker / Runtime

至少覆盖：

- 运行中的 stdout/stderr 能产生 progress callback；
- progress chunk 在发送前 Secret 已脱敏；
- progress 失败不导致 Adapter Execution 失败；
- final result 仍包含最终日志并保持 authoritative；
- 原 M2 timeout/venv/Secret/大字段测试全部回归通过。

### 12.3 Web

至少覆盖：

- dirty Working Copy 禁止运行测试；
- 当前选中的历史 Version 被显式提交为 `version_id`；
- 非法 JSON 阻止创建 Execution；
- 防重复运行；
- SSE fetch 带 Authorization Header；
- 不把 Token 放 URL；
- pending/running/terminal 状态更新；
- Output 完整展示；
- truncated Output 展示 size + preview；
- stdout/stderr 展示；
- 执行历史列表、加载更多、详情；
- M1/M2 Adapter 管理与 Token 登录行为全部回归。

### 12.4 Compose smoke

真实 Compose 流程至少验证：

```text
四服务 healthy
→ Admin Token 登录/API 可用
→ 创建 Adapter + 保存 Version
→ 创建 Manual Execution
→ Worker 领取
→ Adapter 运行期间产生至少一条日志
→ SSE 能观察到 running 与日志事件
→ Execution 最终 succeeded
→ Output 正确
→ 执行历史列表能查到本次 Execution
→ 详情能查到最终日志/Output
```

Smoke Adapter 继续只使用 Python 标准库，避免依赖公网 PyPI。

---

## 13. 明确不做

M3 不包含：

- AI Editor / AI 生成/修改/调试；
- 文件上传；
- RAGFlow / Knowledge Provider；
- Schedule；
- Webhook；
- JavaScript / Java；
- AdapterInstance；
- User / Role / Permission / Session；
- RBAC；
- Redis / RabbitMQ / Kafka / NATS；
- WebSocket；
- 独立日志系统；
- 对象存储；
- Execution 取消；
- 业务任务重试；
- 新数据库表；
- 为 UI 美化进行无关后端重构。

---

## 14. 完成验收

M3 最终必须可以在浏览器完成：

```text
创建 Adapter
→ 编辑 Python
→ Save v1
→ 进入“测试运行”
→ 输入 JSON
→ 点击“运行测试”
→ 看到等待中/运行中
→ 实时看到 stdout/stderr
→ 执行成功
→ 查看格式化 Output
→ 进入“执行记录”
→ 找到刚才的 Execution
→ 打开详情
→ Input / Output / Logs / Duration / Version 全部可追溯
```

同时视觉验收：

- 登录、Adapter 列表、编辑、测试运行、执行记录均完成统一现代化 UI；
- 不再保留明显的裸 HTML/工程 Demo 视觉；
- 中文界面；
- 关键状态一眼可辨；
- Monaco 仍是主要代码编辑区；
- 桌面端主工作区布局稳定可用。

任务完成后创建 PR，等待独立 Code Review；不要自动 merge，不要开始 M4。
