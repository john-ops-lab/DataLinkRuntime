# Design

## Context

动机与范围见 [proposal.md](proposal.md)。现有 manual 路径已有 Execution 幂等记录，取消存在不提交的 locked helper；Subscription 启停服务由调用方提交。Schedule/Webhook 目前由 full PUT 服务提交，不能让浏览器复制旧配置执行批量启停。

Adapter.run_mode 仍只有 manual/schedule；事件方式通过未删除的订阅绑定判定。原目录运行中的执行摘要只含 running，不能代表 queued/retry_wait，因此手动停止需另行冻结全部未完成执行。

## Goals / Non-Goals

**Goals:**
- 复用现有领域校验和锁顺序，在同一事务中提交单项变更及结果。
- 通过轻量操作记录解决刷新、断线和重试，不依赖浏览器一直存活。
- 选择、结果与工作台草稿互不干扰，用户能看清实际动作。

**Non-Goals:**
- 不增加后台工作流引擎、额外依赖、异步无限任务队列或跨项回滚。
- 不改变 Execution Admission、Slot、事件责任、源确认或恢复协议。

## Decisions

### 1. 准备、执行、查询分离

新增以下 API，所有路由沿用 business principal 与 CSRF/Auth：

- `POST /api/adapter-batches`：`{id: UUID, action: start|stop, targets: [{adapter_id, expected_token}]}`，只创建并返回冻结计划。
- `GET /api/adapter-batches/{id}`：返回安全结果及关联对象的当前状态，不执行动作。
- `POST /api/adapter-batches/{id}/execute`：只处理 pending 项，可恢复中断的原操作。
- `POST /api/adapter-batches/{id}/retry`：`{adapter_ids: [...]}`，只重试明确失败、可恢复且仍符合原配置的项。

创建请求以 owner + UUID + 规范请求摘要核对幂等；不同内容使用同一 ID 返回冲突。操作记录不保存 code、input、凭据真值或配置正文，只保存校验摘要、动作、对象身份、错误码和执行关联。准备响应丢失时通过同 ID 查询，不用新 ID 猜测重建。

返回 operation 包含 `id/action/created_at/items`；每项包含 `adapter_id/adapter_name/mode/action/status/error_code/retryable/execution_ids/executions/subscription_id/subscription_state`。mode 使用 manual/schedule/webhook/subscription。状态为 pending/accepted/stopping/succeeded/no_change/failed/unknown；succeeded 仅表示该启停动作完成，不能解释为 Adapter 业务成功。运行一次始终以 accepted 和独立 Execution 状态展示。

结果查询始终绑定原执行/激活身份。旧监听停止后另行启用的新激活不能使旧批次重新停止新监听，也不能用新激活的状态覆盖原停止事实；缺少足够历史证据时呈现待确认，不从当前状态倒推虚假成功。

### 2. 配置标识与权限校验

Catalog/详情增加 `batch_token`，使用批量查询计算已保存运行事实的摘要：类型/运行方式、最新代码版本、输入 revision、Worker/timeout、凭据绑定引用、定时静态配置、Webhook 静态配置，以及订阅 ID/config_version/source_epoch。不包含名称、调度游标、在线状态或运行中的执行状态，避免正常活动造成虚假冲突。仅凭据引用参与摘要，不读取明文。

准备与执行每项持有 Adapter 锁，重新检查当前 ACL 和 token；账号入口还须逐项重新读取账号 enabled、role 等访问状态，不能复用整批请求开始时的管理员角色绕过后续降权。手动停止冻结当时全部 queued/running/retry_wait 的 ID；事件停止另固定当前激活身份，防止旧计划影响后来开启的新激活。权限撤销、删除等返回同一个安全不可见结果，查询不能从旧记录泄露名称或执行详情。凭据只冻结引用，轮换继续沿用既有语义，不承诺冻结秘密版本。

### 3. 串行、逐项原子提交

每批最多 100 个 Adapter。首期每批变更并发固定为 1，满足 Issue 建议最多 5 的上界；采用数据库协调锁阻止同批多个 execute/retry 请求重入，忙时返回明确状态供查询，不无限阻塞。每项独立数据库事务，调用不自行 commit 的领域 helper 后写结果并提交；只有明确的业务/校验错误才能回滚该项后持久化 failed。数据库连接或 commit 异常须先查询原回执，不能写 failed 覆盖可能已经提交的结果。

锁顺序继承 Adapter → 配置/来源 → 按 ID 排序的 Execution/Admission/Slot 等既有顺序。协调锁在请求完成或连接失效时释放，pending 项可由明确继续操作恢复。没有独立后台调度，GET 永远只读；浏览器结果面板可定时查询真实进度。

Schedule/Webhook 新增 state-only 入口或内部不提交 helper，必须基于锁下当前配置应用 enabled，重用原有版本、Worker、输入、权限、路径冲突等门禁。不得在客户端重发旧完整配置。Subscription blocked/repair 等不自动重置或调用恢复捷径。

### 4. 幂等与重试

每项 receipt 与领域变更同事务，manual 还沿用稳定 item identity 的 Execution 幂等键。原操作的成功项不重新提交，即使之后配置或启停状态变化，也保留原受理事实并展示当前状态。

失败重试使用同一冻结配置并再次鉴权。容量/临时门禁等已确认失败可重试；配置冲突、不可见、缺配置、需修复及未知结果不自动重试。双击、刷新、丢响应先查询原 ID；不存在或不确定只允许安全恢复同一请求，不能换 ID 自动启动。

设计参考 AWS Builders' Library 的[安全幂等重试](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)：用调用方请求标识表达意图，拒绝相同标识不同参数，并让结果记录与变更原子提交；本实现仍以 DLR 既有数据库与服务为准。

### 5. 界面沿用目录

目录提供可退出的批量选择状态，Checkbox 与导航按钮并列；全选仅针对 visible，超过 100 提示缩小范围，不截断。只读项禁用。点击启动/停止后展示冻结计划及每项具体动作，再明确执行。结果 Drawer 独立于编辑区与 live watcher，刷新目录与权威状态但不调用保存或自动切到日志。

本地恢复只存操作 UUID，按入口和当前账号隔离；查询服务端再次校验 owner 与每项 ACL。结果未知只显示重新查询；原请求已有明确 pending 时允许继续未完成项。中英文覆盖动作、限制、冲突、受理、停止中和真实终态。

## Risks / Trade-offs

- [响应丢失或 Control 重启] → 单项原子 receipt、稳定 UUID、查询后继续 pending。
- [串行处理速度低于并行] → 100 项有界，单项只提交状态；监听关闭不持有事务等待，查询显示停止中。首期不追求吞吐。
- [配置指纹漏字段] → 对 mode/input/version/worker/bindings/触发配置分别做并发变化回归，批量读取避免目录 N+1。
- [自然结束与取消混淆] → 显示冻结 execution 明细；仅全部实际 cancelled 才称已取消。
- [操作记录增长] → 首期保留轻量回执，不添加不明确的过期后重复执行语义；后续保留治理须独立固定合同。
- [历史文档描述落后] → #223 以当前代码、#219 delta 和本文合同为准，只补本变更使用说明，不借机重写全部历史文档。

## Migration Plan

新增 0050 创建批次与单项回执表。针对性测试使用独立 PostgreSQL 测试库验证 fresh migration 和原子行为。PR 精确 HEAD 四项 CI 通过后，在现有私有编排中备份并向前迁移，保持原库、卷、凭据、挂载和所有非应用服务；核对镜像源码与 schema，执行代表性真实运行及浏览器验收。回退应用时保留新增表和已发生责任，不自动降级数据库。
