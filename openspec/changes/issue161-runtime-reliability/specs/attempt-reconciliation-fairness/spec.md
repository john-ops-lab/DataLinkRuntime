## Purpose

保证持久存在的个别错误数据不能饿死其他过期 Attempt、重试派发和 Hold 到期处理，同时区分确定性行级错误与系统故障，并在有限资源、进程重启和并发条件下保留恢复责任。

## ADDED Requirements

### Requirement: 有界持久扫描提供跨 tick 公平机会
系统 SHALL 使用持久扫描进度及固定轮界，每次最多处理配置批大小 B（1..1000）的候选，不无限扫描或保存无界坏行集合。进度 MUST 与 Attempt 业务 lease、状态和资源责任分离；重启不得回到永久坏行首部。对于两轮内有限 C 个 active 候选且持续完成 tick、数据库及业务锁可用的条件，持续过期正常项 MUST 在不超过 `2*(ceil(C/B)+1)` 次候选保留内获得处理机会；新到高 ID 项不得无限延长当轮。

#### Scenario: limit 为一且最前行永久异常
- **WHEN** 最前坏行不删除、不修复，B=1 且后继正常项持续过期
- **THEN** 后继项在规定界内恢复，坏行 lease/状态/Slot/Admission 均不被伪造修改

#### Scenario: 坏行数量占满或超过批次
- **WHEN** 至少 B 条坏行排在正常过期项之前
- **THEN** 多次 tick 跨越这些行后仍处理正常项，各次候选、日志和内存有界

#### Scenario: 保留候选后崩溃并重启
- **WHEN** 进程在保存扫描进度后、处理候选前退出
- **THEN** 重启延续持久进度，被跳过的未收敛责任在下一有限轮再次可见，不能永久丢失

#### Scenario: 并发和新增候选
- **WHEN** 多个 reconciler 同时运行、租约并发续期且有新高 ID Attempt 到达
- **THEN** 固定轮界保持公平；受锁重检吸收重复、续租和已终态记录，不形成两个 terminal 或重复 Retry

### Requirement: 只隔离明确行级错误并清理事务
系统 SHALL 只隔离白名单中的确定性快照/数据校验错误，逐行 rollback 后继续。数据库连接/事务错误、Admission drift 和未知程序异常 MUST 暴露为 tick 失败，不得被无条件吞掉；错误不得包含凭据、输入、SQL 参数或原始异常内容。

#### Scenario: 行级错误与后续职责
- **WHEN** 一条过期项触发确定性 retry-policy 数据错误
- **THEN** 该行所有业务写入 rollback，后续候选、retry dispatch 和 hold expiry 仍获得本 tick 处理机会，并记录稳定非敏感诊断

#### Scenario: 数据库断连和未知程序错误
- **WHEN** 恢复过程中发生 OperationalError、连接失效或未分类程序异常
- **THEN** 错误继续可见且事务回滚，不声称业务已收敛，不释放无权释放的资源
