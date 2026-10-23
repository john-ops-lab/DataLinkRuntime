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
