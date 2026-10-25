# 普通 Adapter 处理 Kafka 事件

`kafka_to_target.py` 是普通 Python Task，使用 `handle(context, input)` 读取 `dlr.event.v1`，不在 Adapter 内启动 Consumer。依赖仅为 Python 标准库，保持普通 Task 保存的输入独立。

消息 value 必须是 UTF-8 JSON 对象，含稳定 `business_id`，例如：

```json
{"business_id":"order-1001","status":"paid"}
```

目标服务必须在同一事务中按 `business_id` 去重，并拒绝同键不同内容；只有请求头里的 `Idempotency-Key` 不构成幂等保证。示例不自动定义 tombstone 删除语义，不接受任意二进制业务内容。遇到这类数据会明确失败，按事件补处理或 skip 流程处置。

1. 将示例保存为 Python Task 代码，设置并固定 Runtime Worker。
2. 在 Revision 的 `runtime_config` 中配置 `target_url` 和 `timeout_seconds`（最多 30 秒）；默认 URL 使用保留的示例域名，不能直接作为真实目标。
3. 在 Secret Store 创建目标 Token，再绑定为 `EVENT_TARGET_TOKEN`；不要把 Token 放进代码、运行配置、事件或普通输入。
4. 创建停用的 Kafka 订阅，使用独立 group、精确 topic 和明确首次起点。实际来源何时可启用，以对应批次验收及 capabilities 为准。
5. 处理后从目标侧独立查询业务 ID，不能只根据 DLR Execution 成功或 Kafka commit 判断业务完成。

同一业务 ID 的有效写入仅一次，重复请求返回 `applied=false`。如果目标提交后响应丢失，原执行可以失败或结果不明；保持相同业务 ID 和内容补处理，再读回目标核对结果。平台 Inbox 去重保护持久接管，目标幂等保护业务副作用，两者各有职责。

## 隔离验收目标

`tools/subscriptions/target_probe.py` 是测试工具，使用独立 PostgreSQL 数据库，创建 `dlr_subscription_target` 表，不清库或删除数据。配置文件必须在仓库外，包含 `database_url`、`token`、`host`、`port`：

```sh
uv run --project backend python tools/subscriptions/target_probe.py --config /path/to/private/target.json
```

除了无需认证的 `/health`，其余接口需要 `Authorization: Bearer`：`POST /events` 写入、`GET /records?business_id=...` 读回、`GET /stats` 统计。`POST /fault` 支持 `normal`、`unavailable` 或 `lost_response_once`；后两者可限定 `business_id`，响应丢失只在数据库提交后发生。只对任务自己的隔离目标使用该工具，不能作为生产接收服务。

源 commit 表示 DLR 已持久接管，**不表示业务已经成功**。Kafka、目标与 DLR 的真实版本、已通过场景和容量边界应随验收记录一起保存；本示例本身不是验收结论。

# Ordinary Kafka event Adapter

`kafka_to_target.py` is an ordinary Python Task using the standard `handle(context, input)` JSON protocol. It needs no third-party packages and starts no consumer. Message values must be UTF-8 JSON objects with a stable `business_id`. Binary values and tombstones require an explicit business policy.

Configure `target_url` and a bounded `timeout_seconds` in the saved Revision's `runtime_config`. Bind the target token through Secret Store as `EVENT_TARGET_TOKEN`. The target must atomically deduplicate the business ID and reject different content under the same ID; an HTTP idempotency header alone provides no such guarantee. Redirects are refused to keep credentials at the configured target.

The isolated `target_probe.py` tool persists into a separate PostgreSQL table, never resets its database, and supports an authenticated post-commit lost-response fault. Keep its configuration outside the repository. Replay the same business ID after an unknown response, then independently read the target. Source acknowledgment establishes durable DLR custody; it does not establish business success. Source availability and acceptance remain governed by the corresponding batch record.

## Kubernetes 普通 Adapter 与目标对账示例（B4 候选）

`kubernetes_to_target.py` 也是普通 Python Task，不在 Adapter 内启动 LIST/WATCH。它将 `dlr.event.v1` 发送至目标，使用与上例相同的 `target_url`、有界 `timeout_seconds` 和 Secret 绑定 `EVENT_TARGET_TOKEN`。目前用于 B4 后端候选验收；来源的公开可启用状态以 capabilities 和 B4/B5 记录为准，示例存在不代表 Kubernetes 已交付。

目标必须持久实现以下业务合同，不能只看到 `sync.completed` 就执行全表删除：

1. `sync.started` 为本订阅和 scope 建立一次新的 generation，重复请求返回原结果。generation 独立于不透明 RV，不比较 RV 大小。
2. LIST 对象按 UID 幂等写入，保存本批成员及内容事实。目标只有在 begin 有效、generation 仍是当前批次、读取完整、成员数量及当前目标行内容全部吻合时，才允许完成对账。
3. `sync.completed` 的清理和幂等结果须在同一目标事务提交，仅清理本订阅、本 scope 中未被本完整批次看到的旧 generation。`sync.failed` 永久取消该批清理资格，但允许已经接管的旧成员继续幂等写入。
4. WATCH 更新/删除定位 UID；同名新 UID 不受旧删除影响。带 selector 的移除可能只是离开范围，不代表集群对象物理删除。scope 外的数据始终保留。
5. 新批 started 生效后，迟到且尚未生效的旧 completed 必须拒绝清理；携带旧 Batch 的 WATCH 也不能写删新代次对象。已生效控制的重复请求只返回旧结果，不再次清理。平台“放弃完成确认”不等于目标成功，也不能撤回远端请求或回滚部分写入。

隔离工具 `tools/subscriptions/kubernetes_target_probe.py` 用独立 PostgreSQL 数据库保存 scope、batch、成员、对象和控制结果；不清库、不重置 Kafka 目标。它按 `(subscription.id, source.identity)` 隔离管理范围，`source.identity` 为 `kubernetes-scope-v1:<scope_fingerprint>`。没有 Batch 的 WATCH 依赖此前已持久生命周期建立的范围映射，未知范围拒绝写入，不猜 selector。此类事件不携带目标 generation，示例不能从不透明 RV 判断任意迟到同 UID 更新的新旧；业务如需处置结果不明后仍继续写入，须另外定义目标侧顺序/并发条件，不能外推为全局 exactly-once。实际目标若把多个 scope 合并成一份业务表，应另行定义所有权，不能直接去掉此隔离键。

配置文件仍放在仓库外，含 `database_url`、`token`、`host`、`port`：

```sh
uv run --project backend python tools/subscriptions/kubernetes_target_probe.py --config /path/to/private/kubernetes-target.json
```

`POST /events` 接收完整 Envelope，并要求认证与 `Idempotency-Key: <event.id>`。`GET /events?subscription_id=...&source_event_id=...` 查询持久操作结果；业务验收还须独立读取 `dlr_k8s_target_objects` 的 UID、完整 payload、generation 和 writes，以及 scope/batch/member 表，不能只查询操作成功。`POST /fault` 支持 `normal`、`unavailable`、`lost_response_once`，后两者必须限定一个精确 `source_event_id`；响应丢失发生在目标事务提交之后。

### Ordinary Kubernetes Adapter and reconciliation target

The ordinary Python sample forwards the closed event Envelope to a transactional target. It starts no LIST/WATCH loop and retains the normal Task input contract. Configure the bounded target URL/timeout and bind `EVENT_TARGET_TOKEN` through Secret Store. This is a B4 candidate example; consult capabilities and batch acceptance records for public availability.

The isolated target persists per-subscription scope ownership, batch generations, UID memberships, business objects and idempotent event results. Completion checks both membership evidence and current business rows before cleaning only older rows in that owned scope. Failed batches permit accepted members to drain but never regain cleanup eligibility. UID-specific removal preserves a same-name replacement. Generation checks reject late old completions and batch-tagged WATCH writes after a new batch starts; repeated committed controls return their prior result without repeating cleanup. WATCH events without a batch do not carry a target generation: opaque RV cannot establish arbitrary late-update ordering, so additional business concurrency rules are necessary after unknown remote outcomes. This is not global exactly-once. Platform abandonment neither confirms business success nor recalls remote requests.

Keep private configuration outside the repository. Authenticate `/events` and use the event ID as its idempotency key. Fault injection is restricted to one exact event ID and post-commit response loss is observable through independent PostgreSQL reads. Read the actual business rows as well as operation results; an acknowledged control alone is insufficient acceptance evidence. This finite acceptance fixture is not a production ingestion service or a retention implementation.
