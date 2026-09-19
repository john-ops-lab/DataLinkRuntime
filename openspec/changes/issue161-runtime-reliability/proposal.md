## Why

现有可靠执行链路在正常 Worker 满容量时会把容量等待转成关闭连接/重投，已有 infrastructure Incident 缺少人工恢复入口；恢复循环中的单条异常还会长期阻断后继 Attempt、Retry 与 Hold 清理。第一组需要在保留原 Execution、输入快照、ACK-on-claim 与 PostgreSQL 权威的前提下补齐这些责任，并消除 Worker metadata 和取消码的契约漂移。

## What Changes

- #135 A：Worker ORM 补齐已由 Alembic 0031 创建的 Check Constraint / Index；不新增重复 migration。
- #135 B（承接 #136）：正常取消的 `error_code/last_error_code` 统一为 `execution_cancelled`，保留 `cancelled` 状态及全部资源收敛语义，不批量改历史。
- #134：使用持久、有界、公平的候选扫描，逐行隔离明确的数据错误；进程重启、`limit=1` 与坏行占满批次时仍推进，数据库与未知程序异常继续可见。
- #152 A：将 Broker 接收与真实执行槽协调，保留 durable Claim/journal 后 ACK、及时心跳、有限资源及真实故障重连；正常满容量不关闭连接或耗尽 delivery limit。
- #152 B：在既有 Execution 详情增加 infrastructure Incident 的人工恢复/合法终结 UI/API；同一 Execution 恢复、事务内资格重检、独立处置审计/幂等、Outbox 代次与 fencing 均由领域服务负责；修正 x-death 原因分类并保留既有非 delivery-limit 自动流程。
- 作为 #152 旧数据升级验收的必要配套，为固定预览控制器增加显式、狭义的 Incident 保留升级合同；默认空闲门禁不变，不通过取消旧 Execution、清空卷或改状态绕过升级。
- 为完成原 Incident 处置后详情输出可读性的必要修复，增加独立显式 `audited-web-same-schema-v1` / manifest v3：仅 `0040→0040`、闭合 Web 源与配套路径差异，完整保全已完成处置的终态、全部审计及其余 queued/cleanup 责任；原 v2 audit-empty 合同不变，不提供通用带审计升级。
- 串行提交/测试检查点：#135 A → #135 B → #134 → #152 A → #152 B（含升级）；同组一个最终 PR，最终 head 统一 CI、独立 Review、Gate、合并/部署 SHA 核对。用户最终体验验收未完成时 Issue 保持开放。

非目标：#129、其他三个交付组、新自动恢复策略、RabbitMQ HA、增加 Adapter 并发 Slot、修改 ACK 为业务完成后确认、放宽 delivery limit、重放为新 Execution、批量修复历史取消字段或改写 #130 历史。

## Capabilities

### New Capabilities

- `worker-metadata-consistency`：已发布 Worker 数据库对象与 ORM 声明一致。
- `execution-cancellation-code`：统一正常取消终态的稳定机器码与幂等观察。
- `attempt-reconciliation-fairness`：有界持久扫描、逐行异常分类与跨 tick 公平推进。
- `worker-capacity-backpressure`：ACK-on-claim 下真实槽容量与 Broker 接收协调。
- `infrastructure-incident-disposition`：已有 Incident 的同 Execution 人工恢复/终结、状态资格、审计与权限。
- `incident-preserving-upgrade`：冻结无 active Attempt 的特定 Incident 责任，保留原记录完成同机制向前升级；另以显式 v3 支持本组受限 Web 修复的同 schema 审计保全。

### Modified Capabilities

无。当前 `openspec/specs/` 尚未包含 #130 的可靠执行能力；本变更新增的是第一组修复合同，引用并保留 `openspec/changes/issue130-reliable-execution-runtime/` 的原验收，不复制或归档该历史变更。

## Impact

- Backend：`models/execution.py`、`models/reliable_execution.py`、`services/attempt.py`、`execution_cancellation.py`、`execution.py`、`infrastructure_dlq.py`、`outbox.py` 与现有 Execution API/schema；新增扫描游标和人工处置审计的增量迁移，升级不得清空旧 Incident。
- Worker：`worker/consumer.py` 的消息接收、槽预留、连接生命周期和 Claim/journal 调度；不改变 Sandbox、Worker v3 决策或执行 wire payload。
- Web：既有 `ExecutionHistoryPanel` 的 Incident 区域与 API 类型、双语文案、权限展示；继续 React 19 / Ant Design 5.29.3 / ProComponents 2.8.10。
- 部署：`tools/local-preview/` 与双语部署说明；私有地址、配置、备份、真实记录/快照和证据不进入公共仓库。保持固定项目、持久卷、Token、Master Key。v3 不新增 migration，只允许 design 中逐文件列举的差异；仍正式构建/核验全部候选镜像，失败保留 attention。
- 兼容性：新 API/响应字段为增量；不改公开状态枚举，旧 Incident 无需补造处置记录。仅当前 RabbitMQ 机制的向前迁移，不支持 legacy 执行转换或数据库自动 downgrade；失败保留 attention 和备份，按实际 schema/镜像事实处理，不自动启动旧二进制。
- 验证：PostgreSQL 行锁/迁移与并发测试、真实 RabbitMQ 4.3 / Worker / Linux Sandbox、已有数据升级和真实 Chrome UI/API 验收分开记录。测试桩不能替代 Broker、DB、UI 或最终用户验收。
