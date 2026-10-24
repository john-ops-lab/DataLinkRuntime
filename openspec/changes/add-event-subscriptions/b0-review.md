# B0：批次审查结论

2026-10-23，B0 验证及独立只读审查通过，允许进入 B1，无剩余架构阻塞。原始结果与首败分别保存在私有证据目录；此处只记录可公开的结论和验证范围。

## 已核对交付物

- [b0-compatibility.md](b0-compatibility.md)：共享 trigger/JSON/Worker 合同、T1/T2 事务、activation 引用、运行锁旁路、订阅 replay 和清理保护改动清单。
- [b0-clients.md](b0-clients.md)：最终选定 Kafka Python 3.0.11、Kubernetes 35.0.0、urllib3 2.8.0；精确版本源码、Linux 安装和现有冻结后端依赖兼容通过。
- Kafka 真实 identity/assign/seek/commit/pause、二进制/null/header 通过；已消费后既有 TCP 黑洞且存在 pending commit，close 最终约 3.01 秒，通过 5 秒断言。Confluent 消费者的 35 秒超时首败保留为弃选证据。
- 16 个 Kafka Consumer 关闭后线程/FD 恢复预热基线；只声明空闲连接样本，不声明持续负载硬 RSS 上界。
- K8s 真实 Pod/Deployment 分页与 WATCH 通过；原始 LIST/WATCH 在 JSON 解码前拒绝超限数据，弃用直接 Watch.stream 后检查大小的接管路线。
- [b0-budget.md](b0-budget.md)：13 项专用池断言通过；配额与存储下界计算正确。实际支持环境的 DNS/NSS、受控回调、停止先 fence、原生调用结束后才返还 permit 均明确。
- [b0-environment.md](b0-environment.md)：开发测试环境初始化、空库迁移和无订阅的真实 Control HTTP/RSS/数据库基线。

## 进入下一批的约束

B1 必须落实原 activation 引用、T1/T2 原子交接、中心及旁路运行锁、通用 replay 拒绝订阅入口、清理引用保护和控制额度。本文可行性审查不替代对应事务/竞争测试。

TLS/SASL、事务隔离、来源故障恢复、产品持续负载、真实业务目标读回和浏览器交互继续按 B2–B5 的原门槛执行。当前五项勾选仅表示 B0 完成，没有将后续功能标记为已实现。
