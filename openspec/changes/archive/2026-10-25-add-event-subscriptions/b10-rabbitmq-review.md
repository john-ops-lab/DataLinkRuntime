# B10 RabbitMQ 实施者自审

2026-10-24。7 项直接测试通过，其中 1 项使用隔离 RabbitMQ 4.3.5 / AMQP 0-9-1 真实连接；无全量测试。

- 产品连接只 passive 检查已有队列，exclusive consumer、prefetch=1、manual ACK；不创建/清空来源队列。配置要求 durable 专用队列，AMQP passive 响应不能独立证明其 durable 属性，须由来源管理员预先配置。
- 公共队列运行器复用原 T1/责任/额度；提交前 rollback、停止、旧 owner 不接管；确认丢失重投仅增加 duplicate_count，修改正文触发身份冲突。所有来源 RPC 均在 DB 事务外。
- 单连接/单消息，Pika 1.3.2 Header 在正文组装前拦截超限；3 秒 AMQP 操作定时中断、5 秒连接上限、2 秒确认 native close。真实超限场景暴露 Pika 包装异常，修复为确定阻塞码。close 最初使用底层断流未设置 Pika 终止原因，已改为 terminate_stream 并实测关闭。首失败日志保留私有目录。
- 真实队列：诊断未消费；未提交断连后同 message_id 重投；T1 成功后无 ACK 再重投保持一条 Receipt；ACK 后关闭；超限未确认。二进制正文/headers、深度/数量限制、容量阻止建连、无身份诊断、关闭失败保留 native 预算均覆盖。
- SDK 原始日志不进入平台日志；错误只输出有界机器码。代码 Ruff/mypy 通过。TLS 使用系统/指定 CA 且校验 hostname，集成认证与加密补充验证在最终测试批记录。
- 按实现依赖调整批次：本批完成原生与持久接管，B13 在同一部署版本统一检查两来源真实 Worker 结果；该结果在此尚未宣称。UI 未就绪前 API 可用集合仍保留旧两来源。
