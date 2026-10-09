## Purpose

本合同在现有 Worker 缓存、恢复和 RabbitMQ 责任模型内处理已经复现的环境发布及异常隔离问题，使合法依赖可正常运行、受保护缓存不被误删，并使单条可重试异常不会无限阻断其他合法责任的有界推进。

## ADDED Requirements

### Requirement: Safe Python environment promotion
Python 依赖准备 SHALL 在安装成功后对已知安装器私有锁元数据收紧权限再提交缓存，同时保留对其他内容的权限、类型、大小与摘要校验。安装成功与缓存 ready/真实业务成功 SHALL 分别验证。

#### Scenario: Installer creates a writable advisory lock
- **WHEN** 普通源或内置材料安装创建了 world-writable 的私有环境锁文件
- **THEN** 发布前 SHALL 将合法普通锁文件收紧为 Worker 私有权限，使完整性校验和原样业务运行成功；其他 world-writable 内容仍拒绝发布

### Requirement: Corrupt deletion records do not stop healthy recovery
可识别的局部删除记录损坏 SHALL 被隔离报告，扫描和恢复 MUST 在既定预算内推进健康记录；未知 guard、内容或引用继续保守保留，真实 I/O/基础设施错误不能当作成功吞掉。

#### Scenario: Bad records occupy the first page
- **WHEN** 结构错误或破损 JSON 的记录持续位于扫描前页且 limit 为 1
- **THEN** 后续健康记录 SHALL 在有界后续轮次获得处理机会，坏记录和其未知保护状态保留，管理快照能说明局部异常

### Requirement: Cache cleanup and message recovery preserve responsibility
缓存删除/等待与 DLQ 处置 SHALL 在真实可达状态上验证原 Execution、引用、guard、事务和消息去向。可重试缓存等待 MUST 有界推进且不被普通连接故障路径反复消耗投递上限，ACK 前持久化责任与既有 fencing 保持。

#### Scenario: A guarded queued message precedes a healthy message
- **WHEN** 合法可达的缓存 guard 冲突影响 queued 的恢复处置，队列中另有健康消息
- **THEN** 两者的消息/Incident 责任 SHALL 可核对，健康消息有界推进，冲突解除后原 Execution 安全续行；未验证的永久阻塞或消息丢失不得被宣称已证实

### Requirement: DLQ guard conflicts retain an explicit manual recovery boundary
合法 DLQ 消息遇到 `cache_reclamation_in_progress` 时，handler SHALL 持久化 open/manual_review Incident 后 ACK，继续处理健康后继，且不得重新 pending 已有派发行。普通 Claim 的同代延后 SHALL 保留其自动续行机制。

#### Scenario: Published dispatch is reviewed after guard release
- **WHEN** DLQ 对应的 Outbox 已 published，缓存 guard 随后解除
- **THEN** Relay SHALL 不自动再次领取该 published 行；管理员 recover 才按既有策略受控增代并解决 Incident；重复 recover 与重复 Claim MUST 不产生额外责任

#### Scenario: Confirm-before-mark still has pending Relay responsibility
- **WHEN** Broker 已确认原发送但 Outbox 尚 pending，DLQ guard 冲突已记录 Incident
- **THEN** pending 行 SHALL 保留原 Relay 重放责任，解除 guard 后可按原责任继续；人工 recover SHALL 复用当前代并解决 Incident，不被误称为自动续行的前提

### Requirement: Authorized deleted Adapter cleanup is independent of rebuild proof
已删除 Adapter 的环境清理 SHALL 使用 Control 验证的当前 cleanup claim、observed identity 和无未完成引用的 guard 授权，不要求 live-version GC 的可重建证明。普通 GC 的可重建证明与所有 owner、内容、Pin、last-used、local use/journal、fencing 和预算检查 SHALL 保持。扫描耗尽后等待已有删除恢复，不在同一 cleanup 内重复发起删除。

#### Scenario: A deleted Adapter used an environment with unknown rebuildability
- **WHEN** 实际执行已取消且工作区清理完成，Adapter 已删除，环境没有可重建证明
- **THEN** cleanup SHALL 在精确授权和无保护责任时有界删除并记账；活动引用、Pin、内容未知或 stale cleanup claim 仍阻止删除；恢复和重启不重复记账

### Requirement: Bounded cache scan audit identifies actual observations
缓存扫描 SHALL 在现有有界 Worker 日志输出可关联的模式、轮次、预算、分页完整性、游标摘要、已核查安全键、固定原因和释放量；不得输出路径、任意目录名、identity、证明正文、凭据或异常正文。

#### Scenario: Active use spans a periodic TTL scan
- **WHEN** 实际引用跨 TTL 且周期扫描覆盖被测键
- **THEN** audit SHALL 明确关联该键与真实保留原因，解除引用后的安全回收与目录及账目一致；分页局部观察不可标记全量完整
