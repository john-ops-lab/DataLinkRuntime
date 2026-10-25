# B14 RocketMQ 真实账号权限补验与自审

2026-10-24。范围为真实 RocketMQ ACL 验证，以及实测直接阻断既有账号纠正恢复路径的最小修复。审查为实施者自审，未宣称独立审查。没有推送、创建/更新 PR、自动合并、发布、远端 CI 或全量测试。

## 当前结论

- 使用 Apache RocketMQ **5.3.3 / ACL 2.0**，Broker 和本地模式 Proxy 均开启认证、授权，测试请求无白名单。来源与 DLR 内部调度队列分开，旧来源及原业务库保留。
- 正常消费者为 **Normal** 用户，仅授测试消息主题和消费组的 `Sub` 权限；生产者为另一个仅有 `Pub` 权限的 Normal 用户。正常消费者可接收并 ACK，不能发布。管理员只用于配置和权限读回，不充当成功用例账号。
- 错误密码、未知账号、未携带账号、缺少主题权限、缺少消费组权限均由真实服务器拒绝。直接 `ReceiveMessage` 和已取得句柄的 `AckMessage` 也验证拒绝，之后由合法消费者读取原消息并确认。
- 部署后的 DLR 四种拒绝用例均无 Receipt、Delivery、Execution 或目标写入；修正同一凭据引用后，在**同一配置版本和接收周期**成功处理此前未确认的原消息。原 Worker、Revision、输入字节与独立目标 SQL 数据均已核对，目标 `writes=1`。收尾停止接收，pending、native、应用缓冲均为 0。

## 必须保留的协议边界

5.3.3 的 [ResponseBuilder](https://github.com/apache/rocketmq/blob/rocketmq-all-5.3.3/proxy/src/main/java/org/apache/rocketmq/proxy/grpc/v2/common/ResponseBuilder.java#L89-L90) 将 `AuthenticationException` 和 `AuthorizationException` 都返回为 `UNAUTHORIZED`，因此 DLR 对这些真实拒绝返回 `source_authentication_failed`。不能为了区分用例而解析服务器原始文本或伪造 `FORBIDDEN`。

缺主题权限账号使用同一签名成功执行已获准的 Group Heartbeat；缺消费组权限账号成功查询已获准的 Topic 路由，再被实际订阅拒绝。结合独立管理员读取的 Normal 类型和精确授权，证明这些是有效账号缺权限，不是用错误密码冒充权限用例。路由诊断不消费，但也不能证明 Group 可用，已同步到中英文说明。

官方 5.3.3 ARM64 镜像中的 `rocketmq-rocksdb-1.0.2` 没有 Linux ARM64 原生库，开启 ACL 首次启动失败。实测改用同版本**官方 Linux amd64 镜像、本机 QEMU 模拟运行**，校验镜像及全部层摘要；未替换依赖、修改 Broker 程序或关闭 ACL。DLR 仍为 Linux ARM64。真实 ARM64 Broker 及真实 Proxy TLS 与 ACL 组合未验；原协议服务器 TLS 结果保持独立含义。

## 首败、最小修复和恢复

1. 原生测试最初错误地期待权限不足返回独立错误码，真实结果为 3 failed / 3 passed；按官方精确版本源码修正验收预期。额外签名对照最初遗漏 Telemetry 设置，保留失败并修正测试准备，最终 6 项通过。
2. 首次 DLR 验收在四类拒绝后，正常账号仍无法接管消息。公共队列运行器把连接认证错误写成了 `blocked_unaccepted`；新消息被旧故障拦住后，第二次记录故障又抛异常，导致运行时保守标记关闭未确认。故障不是 Broker 吞消息，也不是账号更新未生效；独立核对了 Secret Store 真值相等、Linux 实际接收和未 ACK 的原消息。
3. 仅修正 `subscription_queue.py`：原生内容拒绝使用受控消息错误集合；连接/账号故障只由运行时表达，不毒化消息，包括 T1 后 ACK 的账号拒绝。已有 `source_shard_blocked` 保留首次诊断并正常确认实际关闭，不再二次记录覆盖。T1/ACK、配额、源位置、已接管快照、Kafka/K8s 合同没有变化。
4. 新增 5 项确定性回归在旧代码上全部失败；修复后连同 4 项相关保护测试共 9 passed。覆盖 open/poll/ACK 的账号故障、同周期纠正、首次坏消息保护、真实关闭失败仍保留预算，以及原内容/容量拒绝。
5. 首轮旧版测试订阅无已接管责任。先保存可读备份及故障，再实际停止旧 Control 进程并确认 PID 已退出，通过既有 `listener_closed` 服务核对原 owner/generation 后确认关闭；随后走既有 reset API 留单份审计，保留旧周期故障。未按过期租约假定原生已关，未直接改 SQL 清故障、删除责任、重置 Broker 位置或恢复旧库。
6. 修复后复用**同一个测试适配器和首轮未确认的原消息**重新验证。在新测试周期内连续经历四种拒绝、纠正账号到真实 Worker 成功，配置版本/周期均未再改变。旧周期只作为首败证据，不冒充正常恢复成功。夹具脚本的环境参数、当前周期查询假设及模拟运行资源不足等首次问题均单独保留，没有覆盖首败日志。

## 针对性验证和交付

| 范围 | 结果 |
| --- | --- |
| 原生真实 ACL | 6 passed，包含读取/ACK 拒绝、合法账号及 Sub 不允许 Pub |
| 账号恢复与原坏消息保护 | 旧版新增回归 5 failed；修复后相关选择集 9 passed、5 deselected |
| 原有签名/ACK 状态/TLS 协议服务器 | 3 passed、7 deselected，未冒充真实 Broker TLS |
| 静态与规格 | 改动 Python 文件 Ruff；公共队列模块 mypy；OpenSpec strict、diff whitespace |
| 实际应用 | 4 类拒绝零接管/执行/目标写入；正常账号真实 Worker、原消息与独立目标读回；原输入保留，原故障和 reset 审计保留 |

实测产品提交为 `5210dcd`。从精确 Git archive 仅覆盖变更的后端源码，保留锁定的 90 个发行包和原 Web 产物；核对全部源码、依赖和前端文件，继续使用同一开发编排与六个命名卷、Token、Master Key。备份可读，迁移版本仍为 0049，原业务库及非应用容器保持。

最后收尾提交只更新说明、任务和审查材料；交付流程以逐文件一致性复用已验证产品，从精确收尾 HEAD 再封装同一镜像并部署，另记录部署后的新消息验证。具体 SHA、镜像、来源账号、端点、备份和业务记录仅存私有交付账本，公开材料不复制这些环境参数。

未运行其他来源的真实故障矩阵、Web 全量回归、全语言或持续容量测试；没有 UI/依赖/迁移变更。无远端 CI、独立审查、生产发布或用户验收结论。保留一个停止接收且无积压的 ACL 开发示例、必要来源配置和首败证据，清理无用临时管理容器与源码副本。
