# B2：Kafka 接收、确认和恢复审查

实现提交：`e0b6799`。协议、公共运行时及真实业务结果的独立审查通过。公开可启用来源仍为空；Kafka 的管理界面、持续容量和产品交付属于 B3。

## 验证范围

| 范围 | 结果与边界 |
| --- | --- |
| 公共与 B2 组合回归 | 真实 PostgreSQL 上 99 项通过、11 项真实来源用例跳过；跳过项另行使用隔离来源执行，不把跳过写成通过 |
| Kafka 专项 | 30 个不同用例通过，含 22 个数据库、owner 和本机 SDK 用例，以及 8 个真实 Kafka 用例；覆盖两种首次起点、二进制/null/header、专用组、保留缺口、UUID 改变、TLS/SCRAM 和同引用凭据轮换 |
| 独立运行时及故障审查 | 19 个不同 pytest 用例通过，与上述组合及专项有重叠，不累加；覆盖 16 名额、取消、关闭、租约、旧 owner、API 响应及真实 API 8 的三个故障窗口 |
| 真实事务探针 | 8 项断言通过：实际提交、中止、再提交后，产品 `read_committed` 只读取应可见记录，保留合法 offset 空洞；该探针不是 Worker 业务验收 |
| 静态和配置检查 | 后端完整 Ruff、435 个文件格式检查、211 个源文件 mypy、OpenSpec strict、Compose 示例配置通过；公开默认开关为 false |
| 公开提交检查 | staged diff、Gitleaks 和新增内容的私有地址、用户路径及环境端口复核通过；原始环境和证据保留在私有目录 |

真实故障窗口分别丢弃 OffsetCommit 请求、丢弃 broker 已处理的响应，以及延迟旧 owner 请求至 fencing 后。以数据库安全位置、确认位置、真实 broker 提交和 Receipt 数分别作 oracle：不能把超时解释成未提交，晚到 RPC 不能更新新代次的确认位置；重启从已接管责任恢复且不重复创建 Receipt。

独立审查发现：向 SDK logger 直接挂接 handler 时，仅添加 NullHandler 和关闭传播不能阻止凭据及原文进入该 handler。已在 SDK 源 logger 过滤原始记录并独占该命名空间的 sink，不关闭共享 handler、不改变 root 日志。真实 SDK 回归显式覆盖父、子 logger 直接捕获及构造失败回收；原失败顺序复验通过，平台有界机器码仍可见。

## 精确源码部署与目标读回

开发 Control/Worker 对应精确提交 `e0b6799`。354/304 个运行文件及 manifest 逐一匹配源码快照；安装的 70 个 distribution 版本与锁文件匹配，新增 Kafka 客户端为 3.0.11。使用私有离线基线 overlay 和经官方 SHA 校验的 wheel，不宣称公共 Dockerfile 在线完整构建通过。原始数据库、开发数据库、固定六个持久卷、密钥和非应用容器均保留。

Kafka 4.1.2 的独立双分区消息经真实 Receiver → T1 → T2 → RabbitMQ → Worker → PostgreSQL 目标完成验收。目标以预先不存在的业务 ID、完整 payload、源事件 ID 和有效写入次数作独立 SQL 读回：

- A 首条业务失败时，A 后条已经持久接管但没有 Execution；B 两条业务仍能写入目标。恢复目标并补处理后，A 后条排空，原失败 Execution 保留。
- 目标事务提交后故意断开响应，原执行失败；补处理相同业务 ID 后返回已存在，SQL 证明只有一次有效写入。Kafka 的安全、确认及 broker 位置按具体分区逐项核对一致。
- 暂停开发 Worker，实际 Attempt 租约过期并记录 `worker_lost`、Execution 进入 `retry_wait`。该窗口 A 后条仍没有 Execution，B 目标各写入一次。恢复后，同一 Execution 的更高 Attempt 成功，原 `worker_lost` 不改写，A 后条随后成功。测试仅在私有配置使用 30 秒重试退避、零抖动，未改变公开默认值。

独立复核选定业务样本：9 个目标业务 ID 各写入一次，9 个 Receipt 最终成功，11 个 Execution 中 9 个成功、2 个原始 `dead_letter` 保留；全部经过真实 RabbitMQ。两订阅最终停用、监听关闭，安全位置等于确认位置。

## 必须保留的延迟记录

Worker 中断探针的原 100 秒完成等待**超时**，未改写为按时通过。后续只读核对确认原责任最终收敛：第二代 Outbox 比 `next_attempt_at` 晚 125.897 秒建立，发布至 Worker 领取不足一秒，第二次执行约 4.224 秒成功。延迟位于 Control 重试调度阶段。

保留日志中没有调度异常；持久缓存回收阻塞已排除，历史锁证据不足以证明 `SKIP_LOCKED` 饥饿。原因仍未确定，不能归结为单纯探针窗口不足。K02 规定屏障、跨分区继续及恢复，未规定 100 秒启动 SLO；独立审查未发现本次违反数据或顺序合同，允许关闭 B2 功能门禁。B3 持续速率和延迟验证必须跟踪此现象，不自动扩展为既有 Runtime 大修。

## 支持边界与后续门禁

固定客户端为 `kafka-python==3.0.11`。TLS/SCRAM 的确定错误通过受测内部 typed observer 捕获，外围 timeout 不一概归为认证失败；客户端升级必须复验该内部接口和资源回收。

Metadata topic UUID 在接管非空 poll 前刷新；SDK Fetch 最大 v12 没有原生 UUID fence，不能承诺全部删除重建竞争窗口。Checkpoint 保守取实际记录的下一位置，不用 poll position 跨过控制或中止 marker；retention 因此可能触发保守的历史缺口，不能直接认定业务数据丢失。

来源 owner 与诊断共享最多 16 个名额，无等待队列；原生关闭确认后归还。应用缓冲、SDK frame/fetch 限额、进程 RSS 和 OS/NSS 等待是不同边界，持续资源及 API p95 仍须 B3 实测。B3 通过后才将 Kafka 标记为可交付，最终统一 PR 仍须通过其精确 HEAD 的托管 CI，不自动合并。
