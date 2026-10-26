# 业务批次参考 Adapter

`reference_adapter.py` 同时保留原单条输入/输出，并支持 `dlr.event.batch.v1`。通过普通 Python Adapter 版本 API 保存代码，依赖固定为 `psycopg[binary]==3.3.4`，凭据绑定 `TARGET_DSN`。保存版本的 `runtime_config.event_batch_schema` 必须显式设置为 `dlr.event.batch.v1`，HTTP/RabbitMQ 来源显式选择 `mode=business_batch`；仅在 Python 文件中加常量不能替代保存版本声明。旧 `per_event` 默认不变。

批次来自平台冻结的完整输入；最多 100 成员，完整 UTF-8 输入不超过 256 KiB。示例核对固定 schema、批次标识、manifest 哈希形状、订阅元信息、整数身份、连续索引及重复身份。`manifest_hash` 原样回传，由 Control 对照原持久 manifest 校验；示例不会用不完整信息另算一个哈希。

每个成员沿用单条业务格式与 `target.sql`：HTTP `data.payload` 或 RabbitMQ base64 JSON，业务字段 `run_id/message_id/seq/value`。完整业务对象规范哈希与 HTTP 原文字节哈希仍是两个口径，目标核验不能替代原文核验。目标按 `(run_id,message_id)`、哈希、序号及转换值逐条核验，每成员单独提交事务；同一次 Invocation 可复用一个连接，Invocation 结束关闭，不跨执行常驻。

N 个成员有 N 次必要首次事务；其外整个 Invocation 最多共享 19 次额外重试，不能给每成员重新分配 19 次。所有导入、连接、事务、重连、退避共享从 `handle` 开始的 120 秒总时限；每次连接/事务至多 5 秒且受剩余时间约束。重试白名单和退避仍沿用单条规则。权限、语法、数据与身份冲突立即停止，密码交换后无法证实连接成功也保守停止，不能断言该未知握手故障一定是权限错误。

提交结果不明只按原业务键恢复当前成员；当前 Invocation 内已确认前缀不会因重连重写。首个失败或不明之后，后续全部 `not_run/predecessor_failed`，不会越过错误继续处理。外部取消直接传播或由普通沙箱终止进程，不伪造取消后的成员成功；平台未收到合法结果时按原恢复协议判定未知。平台重新执行完整输入时，目标稳定键仍须防止重复有效效果。

批次结果严格只有四个根字段 `schema/batch_id/manifest_hash/members`；每成员严格六个字段 `receipt_id/delivery_id/delivery_ordinal/member_index/status/error_code`，不附加单条输出或 timing。结果为 `dlr.event.batch.result.v1`，完整 compact UTF-8 JSON 不超过 `min(65536,512+N×512)` bytes，单成员也不超过 512 bytes。只有全部成功，或成功前缀、一个失败/未知、未执行后缀。普通单条输出字段及 timing 保持。

额外证据由最多 64 KiB 的安全日志记录：每次失败仅固定原因/SQLSTATE/错误类型和尝试编号；每成员成功仅身份、业务哈希、转换值、真实 `COMMIT` 返回 monotonic 时间及共享重试计数；最后记录总额外重试数。日志没有凭据、DSN、SQL、正文或自由异常文本；日志不是持久责任或恢复权威。目标原子尝试表只计已提交事务，不把网络失败记成目标提交。真实目标效果仍须使用独立 PostgreSQL oracle 逐条核对，平台事实由 Receipt/Delivery/Execution/Attempt 另外核对。

故障探针 `target_after_commit_before_result` 对批次不能从结果协议读取成员 timing；使用 [fault-probe.md](fault-probe.md) 的 `result_proof_mode=external-oracle`，按明确 execution/run 匹配，由独立目标 oracle 验证提交。诊断 receipt 仅为证据，不参与产品恢复，也不替代 Control 的结果提交。

针对性测试覆盖 100 成员 exact 结果、真实连接复用与独立提交、首个真实权限失败后的前缀/后缀、共享重试耗尽、总时限、取消以及真实成员提交后客户端丢结果。小样本测试不是一万条容量或 60 秒真实故障验证；正式轮次另登记实际代码 SHA、资源、来源原文、全量目标效果和平台责任对账。
