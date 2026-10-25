# B11 RocketMQ 实施者自审

2026-10-24。9 项协议/真实来源测试及 1 项 TLS/签名诊断测试通过；实施者自审，无全量测试。

- Apache RocketMQ 5.3.3 arm64 真实 Proxy：专用 NORMAL topic/组、单条接收、提交前无 ACK 重投、提交后 ACK 丢失重投、稳定身份、明确 ACK 及实际设置流关闭通过。一条持久 Receipt，临时 handle 确实改变且不冲突。
- 设置流反复更新三次，旧迭代器确定退出；最终 close 等待已退出。短 RPC 3 秒、Receive 8 秒、设置流 30 秒，单 channel、最多四帧、2 MiB 原生帧；不实例化高层客户端/调度器/指标 exporter。产品不可见时间固定 30 秒，不续租来源消息。测试缩短至 10 秒加速重投。
- 首轮暴露 Proxy 不接受 5 秒不可见时间；改为测试 10 秒，产品 30 秒。另一轮测试独立长耗时超过测试租约，补齐测试续租以模拟实际 runtime 的独立续租线程；未放宽产品 fencing。首失败日志保留私有目录。
- 临时 receipt_handle、attempt、queue_offset 不进入稳定内容；GZIP 与原文一致，摘要错误、未知编码、压缩炸弹、FIFO、无身份拒绝；ACK 还检查逐条结果，不能以整体 OK 覆盖单条失败。
- TLS/签名测试在本地 gRPC 协议服务器验证 CA 信任、无信任拒绝、正确签名与错误签名拒绝，诊断未创建消费者。此项不宣称真实 Broker ACL 已验收；真实 Broker 当前为隔离无认证 Proxy。仅支持标准 MQv2-HMAC-SHA1 与标准 TLS，不承诺云厂商专用认证。
- 诊断仅 QueryRoute，不接收、ACK、变更组位置；source_error 不带远端文本。Ruff/mypy 通过，实际 Worker 结果留 B13 部署后统一读回。
