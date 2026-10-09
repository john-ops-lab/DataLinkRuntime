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
