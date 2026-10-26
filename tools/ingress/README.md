# 持久接收参考目标

`reference_adapter.py` 是普通 Python 单条 Adapter 示例，通过原版本 API 保存，并显式配置依赖 `psycopg[binary]==3.3.4`、凭据绑定 `TARGET_DSN`。不是平台默认重试，也不修改冻结的 B0 示例。

输入支持 HTTP `data.payload`，以及 RabbitMQ `data.value` 的 base64 JSON；业务对象包含 `run_id`、`message_id`、`seq`、`value`。每次连接、每次事务独立，不复用跨执行连接。轻量转换为 `2 * value + 1`，目标以 `(run_id,message_id)` 唯一；重试同键同内容只读回原结果，不同内容明确失败。

重试只允许连接故障、SQLSTATE `08` 类、`40001` 和 `40P01`。权限、语法、数据或身份冲突立即失败。最多 20 次，整个目标重试窗口 120 秒，每次连接/事务至多 5 秒且受剩余总时限约束；基础退避 0.5 秒翻倍到 8 秒，乘 0.8–1.2 抖动。目标提交结果不明仍使用原键恢复。普通沙箱取消会终止进程及等待，不额外增加针对业务失败的自动 Execution 重试。

密码交换后、连接尚未建立的失败，libpq 可能不提供 SQLSTATE。示例按结构字段保守停止，避免错误账号被盲目重试；不承诺这个握手窗口内未知中断的自动恢复。连接已经建立后的网络或提交结果不明仍按原键重试。

DSN 需提供数值 `hostaddr`，或直接用数值地址作为 `host`，以免 DNS 线程在取消后继续占用。TLS 可保留原服务域名 `host` 来验证身份；不关闭证书校验。真实地址和凭据只放私有配置，不能写入本目录。

目标表为 `dlr_ingress_results` 和 `dlr_ingress_attempts`，与 B0 表分开。`payload_sha256` 是完整业务对象的 UTF-8 规范序列化哈希（排序键、无多余空白、禁止 NaN），不是 HTTP 原文 SHA-256。正式 oracle 必须另外逐条核对发送原文与 Receipt 原文字段/哈希；不能用业务哈希代替原文核验。目标尝试表只记录已提交事务，连接失败等尝试另由有界安全日志及执行输出计数；不能把目标表条数当作所有网络尝试数。

本示例的幂等验证只适用于约定的 PostgreSQL 目标及事务，不为任意外部系统宣称 exactly-once。阶段测量仍须记录实际源码 SHA、重试是否发生和完整链路结果。

## 有界发生器与独立目标核验

`workload.py` 使用仓库外的私有 JSON 配置，命令为 `generate`、`prepare-target`、`publish`、`http`、`verify`。公共必填字段是 `run_id`、`kind`、`count`、`input_path` 和 `output_path`。输入和所有运行证据路径必须在仓库外；已存在的清单/结果/明细文件一律不覆盖。每个正式、预热或故障轮次使用新的 `run_id`。

| kind | count | 用途 |
| --- | --- | --- |
| `warmup` | 20 | 独立预热 |
| `baseline` | 200 | 完整链路对比 |
| `capacity` | 10000 | 正常突发 |
| `fault20` | 20 | 单一故障点 |
| `fault1000` | 1000 | 目标中断恢复 |

`kind/count` 必须显式匹配，截断或篡改的清单拒绝运行。每条原文精确 2,048 bytes，ID 为 `<run_id>:000001`；完整业务对象的 `payload_sha256` 与原文 `raw_sha256` 分开保存。`verify` 仅核对独立目标，原文与平台持久责任仍需另行逐条对账。

```sh
python3 tools/ingress/workload.py generate --config "$PRIVATE_RUN_CONFIG"
python3 tools/ingress/workload.py prepare-target --config "$PRIVATE_RUN_CONFIG"
python3 tools/ingress/workload.py publish --config "$PRIVATE_RUN_CONFIG"
# HTTP 轮次使用 http，不能把两种来源计为同一轮。
python3 tools/ingress/workload.py http --config "$PRIVATE_RUN_CONFIG"
python3 tools/ingress/workload.py verify --config "$PRIVATE_RUN_CONFIG"
```

RabbitMQ 配置另需 `amqp_url`、`queue`。外部专用持久队列固定 `x-max-length=12000`、`x-max-length-bytes=67108864`、`x-overflow=reject-publish`，已有队列参数不符会拒绝开始。固定 Pika 1.3.2、32 个 confirm 在途，逐条消息 ID 映射 ACK/NACK/return，开始发布后最多 5 秒突发窗口；来源确认不代表 DLR 已持久接管。失败或不明时不自动重新发布，保留首败与未确认身份。

HTTP 配置另需 `ingress_url`、`token`；地址必须是该私有配置中的 `/api/ingress/{public_id}`，不跟随重定向或响应提供的其他 URL。`http_concurrency` 可显式设为 1–128，缺省 128；每请求至多 5 秒，所有 ID 首次发送必须在 5 秒内开始，未赶上窗口的 ID 明确记录，不延长首轮窗口。整轮 60 秒，每 ID 至多 20 次 POST。429/503 遵守整数 1–30 秒 `Retry-After`；非法值记为合同失败，使用有限 1 秒兜底。实际 HTTP 请求逐个申请在途名额，读取完响应即归还；退避、排队及等待首轮屏障不占用名额。只有全部 ID 的第一次请求已实际开始或已记录 `not_sent`，才允许查询或重试；超时不重置首轮起点。超时/结果不明保留原编号和原文重试；只有已知道 `receipt_id` 时才查询私有配置下的该记录，不猜测其他记录。有效响应、首轮成功/拒绝/不明、查询、放大量及每 ID 结果分别记录。

目标命令另需 `target_dsn`，只执行本目录 `target.sql` 的非破坏性建表及只读 oracle。核验目标每 ID 的业务哈希、转换值、有效写入恰为 1、已提交尝试数量/ordinal/inserted 与 duplicate 对账；结果和尝试用服务器游标流式读取，不一次载入最多 64 万条尝试。

每轮清单上限 40 MiB、RabbitMQ 明细上限 8 MiB、HTTP 明细上限 64 MiB、每份结果上限 4 MiB，超限停止并保留已有证据。HTTP 不复制请求正文/令牌到明细，按序号关联固定清单；最多 20 万条 POST 结果加聚合计数。发生器须在显式 512 MiB 容器预算内运行，实际资源与软件摘要另存私有 manifest。单测不代替该真实资源验证。

发生器只提供自身 monotonic 原始边界。平台真实请求到达/持久提交时间及跨虚拟机时钟误差由验收控制器单独取证；不能将未经校正的 UTC 差、客户端 202 响应或 Broker confirms 当作精确 `T_accept−T0` 或容量通过。
