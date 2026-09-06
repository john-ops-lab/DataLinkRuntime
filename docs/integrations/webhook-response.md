# Webhook 响应模式（Issue #137）

Webhook 仍由 `POST /api/hooks/{public_id}` 接收，使用绑定的 token Credential 进行 Bearer 认证，
通过现有 Admission、RabbitMQ、Worker 执行。无需新增 HTTP Adapter 类型。

## 配置

在 Webhook 的「运行设置」中选择响应模式，保存后开启接收。

| API 字段 | 默认值 | 约束 |
| --- | --- | --- |
| `response_mode` | `accepted`（接收后返回） | `accepted` / `completed`（处理完成后返回） |
| `response_timeout_seconds` | `30` | 整数 1–300 秒；仅 completed 模式等待时使用 |

`GET/PUT /api/adapters/{adapter_id}/webhook` 读写这些字段。旧客户端 PUT 省略字段时保留已有值。
迁移后已有 Webhook 保持 accepted；新建默认为 accepted。Clone 复制模式和时限，并保持停止接收。
接收中或运行锁生效时不能修改这些字段；停止接收操作本身不修改模式或取消已接收执行。

响应策略在首次接收事务中保存到 Execution 的 `webhook_response_snapshot`，与 Execution/Outbox
一起提交。幂等重试沿用原执行和原响应策略，后续修改配置不会改变它。升级前没有快照的历史执行按 accepted 返回。

## 调用与响应

```bash
curl --max-time 35 -X POST "$DLR_URL/api/hooks/ticket-to-cmdb" \
  -H "Authorization: Bearer $WEBHOOK_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: ticket-2026-001' \
  -d '{"resource_id":"host-001","name":"web-01"}'
```

默认模式立即返回 HTTP 202，原响应格式不变：

```json
{"execution_id":123,"status":"accepted"}
```

completed 模式等待逻辑 Execution 的终态，包含排队、执行和重试等待。单次 Attempt 失败进入
`retry_wait` 时继续等待。HTTP 等待超时与适配器的执行超时独立，前者不会终止后者。

| HTTP | 条件 | 响应关键字段 |
| --- | --- | --- |
| 200 | Execution 成功且输出完整 | `execution_id`, `status: succeeded`, `output`（原 JSON 值，含 null/数组） |
| 422 | Execution 最终失败（dead_letter） | `error.code`, `error.message`，以及 execution_id/status |
| 409 | Execution 被取消 | 同上，status 为 cancelled |
| 504 | Execution 过期或执行超时 | status 为 expired/dead_letter，error.code 为具体执行错误 |
| 504 | HTTP 等待时间耗尽 | status 为 waiting_timeout，error.code 为 webhook_response_timeout |
| 502 | 输出超过存储上限 | status 为 succeeded，error.code 为 output_too_large；不把截断预览冒充完整结果 |
| 410 | 等待期间结果已被清理 | status 为 unavailable，error.code 为 execution_result_unavailable |

原有入口拒绝规则不变，包括无效 Token、停止接收、非法 JSON、输入大小和 Admission 门禁；
这些拒绝仍使用现有 `detail` 错误结构，不创建新 Execution。幂等重试也必须通过入口认证，
停止接收的路由不提供历史结果查询。终态响应仅返回 Worker/Control 已脱敏的结果字段，不返回日志或凭据。

等待时采用异步轮询；每次查询独立创建并关闭 Session，不跨轮询持有事务、行锁或数据库连接。
客户端断开时停止观察，已经提交的 Execution 继续执行。超时后若需要继续等待，应在路由接收中时，
使用相同 `Idempotency-Key` 和相同 JSON 内容重试。更换 key 或不提供 key 会创建新执行；
同 key 不同内容返回 409。幂等保证受现有 key 保留期限约束。成功接收不等于对外写入天然 exactly-once，
适配器的下游写入仍应使用工单或资源业务键实现幂等。

仓库的 Token 与账号 Nginx 入口均为 hooks 配置 330 秒读取超时，覆盖最大 300 秒等待。
自有网关、负载均衡和调用方的读取超时也应大于响应等待时限。

## 工单校验示例

HTTP 200 表示适配器执行成功，**不代表工单业务校验通过**。DLR 保留适配器 JSON 结构，
不从 `ok`、`status` 等任意业务字段推断 HTTP 状态。调用方需要检查 `output.ok` 等双方约定的字段。
例如缺少唯一标识时，本次调用直接返回字段错误，适配器应在校验通过后才调用 CMDB。

```python
def handle(context, input):
    if not isinstance(input, dict) or not input.get("resource_id"):
        return {
            "ok": False,
            "errors": [{"field": "resource_id", "message": "资源唯一标识必填"}],
        }
    # 此处可继续格式校验、字段映射，并在全部校验通过后调用 CMDB。
    # CMDB 仍负责自己的数据约束；下游返回错误时可转换成约定的业务错误结构。
    return {"ok": True, "resource": {"id": input["resource_id"], "name": input.get("name")}}
```

缺失字段得到 HTTP 200：

```json
{"execution_id":124,"status":"succeeded","output":{"ok":false,"errors":[{"field":"resource_id","message":"资源唯一标识必填"}]}}
```

进程异常、资源限制、执行超时等运行失败返回相应非 2xx 和平台错误；进程非零退出可能只有退出码摘要。
需要具体业务字段错误时，按上述方式显式返回结构化校验结果，勿依赖异常堆栈或日志作为外部响应合同。
