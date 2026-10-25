# B9 扩展合同自审

2026-10-24，实施者自审。仅本地提交，未推送或触发远程 CI。

- 受控 registry、封闭配置、能力声明、公共消息身份、按范围唯一组键、0049 前向迁移已完成。未登记来源拒绝；新运行器尚未交付时不对外开放。Kafka/K8s 的身份、哈希、批次完成和历史失败保留原契约。
- Python 3.13 环境安装并锁定 rocketmq-python-client 5.1.2、grpcio 1.84.1、protobuf 7.36.2；Pika 保持 1.3.2。uv lock/check 与依赖检查通过。官方高层客户端的无界 channel 和后台线程不符合本项目预算，改用官方协议定义加 DLR 有界同步 transport。
- 隔离 Apache RocketMQ 5.3.3 arm64 的真实 QueryRoute、设置流、Heartbeat、单条 Send/Receive、手动 ACK 已通过；首轮暴露必须登记客户端、最小长轮询 5 秒，已修订 D13。故障关闭、TLS 和完整 Worker 结果留在 B10/B11/B13，不以探针代替交付验收。RabbitMQ 使用 AMQP 0-9-1；已有 Pika 依赖无升级。
- 94 项针对性测试通过：新配置/Envelope/额度/身份、管理、T1/T2、切换和普通任务兼容，附 Kafka 二进制、K8s UID/完成放弃/闭合重放代表用例；3 项迁移检查通过，包含旧配置原样保留和有新类型时阻止不安全 downgrade。无全量测试。
- Ruff、受影响 7 个模块 mypy、diff whitespace 检查通过。首轮测试环境缺少测试用 Rabbit 参数已纠正，首失败日志保留私有测试目录；测试库与开发库分离。
- 待后续门槛：两来源 native close/重投与 Worker、表单与能力交集、同一开发环境精确本地提交更新。
