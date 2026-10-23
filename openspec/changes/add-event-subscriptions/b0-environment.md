# B0：开发测试环境与产品基线

2026-10-23 用户授权将原人工验收环境改作开发测试。复用原 Compose 项目、私有 Token/Master Key 和六个持久卷，保留原数据库及其独立备份；新开发数据库从空库完成向前迁移。未改变其他项目容器。测试入口、凭据、主机参数、配置及原始证据只存私有目录。

## 初始化结果

- 构建源码：`97b274e47d37e879871ecd144ba9a67dab5675e8`，产品源码等同 `eedda36f9ed1e3ab4f84b0a76532d3916f8ca6d5`。
- Control、Worker、Web、account-web 更新；PostgreSQL、RabbitMQ 和文档服务复用现有实例。
- 新开发库迁移到 `0046_issue166_retire_ai_history`，初始 Execution/Outbox 为零。
- 更新前原库无 queued/running/retry_wait，RabbitMQ 的执行队列和基础设施 DLQ 均为空；八个 DLR 服务初始化后均运行且健康。
- 隔离来源：单节点 Apache Kafka 4.1.2、K3s v1.35.9-k3s1 的 API server；只用于开发和协议验证，不作为生产拓扑或来源 HA 证明。
- VM 预算：6 CPU、8 GiB 内存；来源容器另限制 Kafka 1 CPU/1 GiB，API server 1 CPU/1.5 GiB。

## 未加入订阅前的真实 Control 基线

100 次真实 HTTP `/api/health`、并发 4，全部返回健康，无请求失败：

| 测量 | 结果 |
| --- | --- |
| HTTP p50 | 6.05 ms |
| HTTP p95 | 10.09 ms |
| HTTP 最大值 | 22.94 ms |
| Control RSS | 146368 kB |
| Control 线程数 | 17 |
| 新数据库物理大小 | 11115543 bytes |
| Execution 空表及索引大小 | 90112 bytes |
| Outbox 空表及索引大小 | 40960 bytes |

这只是初始化和无订阅产品基线。专用池隔离结果见 [b0-budget.md](b0-budget.md)，真实客户端结果见 [b0-clients.md](b0-clients.md)。B3/B5 必须在实现后重新记录同硬件预算下的 HTTP/RSS、每事件物理数据库/日志增量、持续完成率和触限恢复；当前健康结果不能代替来源到目标的业务验收。
