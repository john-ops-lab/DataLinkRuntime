## Purpose

定义 Schedule 在统一可靠队列中的显式 misfire policy、有界 catch-up、Admission 满载处理、游标推进与审计合同，使每个计划点都能被可靠入队、明确合并、跳过或过期，而不会无记录丢失或形成热循环。

## ADDED Requirements

### Requirement: Schedule 保存显式 Queue Policy
每个 Schedule SHALL 保存 `misfire_policy=coalesce_latest|queue_every_occurrence|skip_while_busy`、`max_catchup_count` 与 `max_catchup_age_seconds`；默认 MUST 为 `coalesce_latest`、100 与 86400 秒，count 范围 1..1000、age 范围 60..604800。旧 Schedule 迁移 MUST 确定回填默认值且幂等。

#### Scenario: 升级已有 Schedule
- **WHEN** migration 处理没有新 policy 字段的历史 Schedule
- **THEN** 它回填 coalesce_latest/100/86400，不改变 cron、timezone、enabled 或 next_run_at

#### Scenario: 保存非法 Catch-up
- **WHEN** 客户端提交超出范围的 count/age 或未知 policy
- **THEN** Control 返回 422，Schedule 与 cursor 保持不变

### Requirement: Coalesce Latest 只承担最新合法计划点
`coalesce_latest` SHALL 在一个或多个 due point 存在时只为当前最新合法点创建 Execution；较早点 MUST 记录 coalesced 审计。若 Admission 未接受最新点，Scheduler MUST 不把它标记为已承担，而应在容量恢复后重新计算当时的 latest due。

#### Scenario: 停机期间错过十个计划点
- **WHEN** Scheduler 恢复且 policy 为 coalesce_latest
- **THEN** 只创建最新计划点的一个 Execution，并以逐点或可验证聚合记录其余九点为 coalesced

#### Scenario: Adapter Admission 满
- **WHEN** latest due 因 adapter_queue_full 未能创建
- **THEN** Scheduler 不推进该责任边界，记录 blocked 事实并在后续 tick 重新计算 latest due，不热循环告警

### Requirement: Queue Every Occurrence 逐点可靠入队且有界
`queue_every_occurrence` SHALL 按计划时间升序为每个仍在 catch-up count/age 范围内的点创建独立 Execution；Admission 未成功时 MUST 停在第一个未承担点，不得越过它继续创建后续点。超出 count/age 的旧点 MUST 记录 expired/catchup_limit 后才可推进。

#### Scenario: 三个 due point 容量足够
- **WHEN** policy 为 queue_every_occurrence 且三个点均在 catch-up 范围
- **THEN** 系统创建三个具有唯一 scheduled_for 的 Execution，并把 cursor 推进到未来点

#### Scenario: 第二个点遇到容量满
- **WHEN** 第一个点提交成功但第二个点 Admission 失败
- **THEN** 第一个为 enqueued，cursor 停在第二个未承担点，第三个本轮不得越过创建

#### Scenario: 超过最大 Catch-up 条数
- **WHEN** due points 多于 max_catchup_count
- **THEN** 超出可排队窗口的旧点以可验证范围记录 expired/catchup_limit，最多创建配置数量的 Execution

### Requirement: Skip While Busy 明确消费计划点
`skip_while_busy` SHALL 在 Adapter 存在 `queued/running/retry_wait` RabbitMQ Execution 时不创建新 Execution，并记录 `skipped/adapter_busy` 后推进；Global/Outbox Admission 不可用时 SHALL 记录 `skipped/admission_full`。没有这些条件时 MUST 复用统一 ingress 创建 Execution。

#### Scenario: 前一 Execution 仍 queued
- **WHEN** 新计划点到达且同 Adapter 已有 queued Execution
- **THEN** 系统不创建第二条，记录 skipped/adapter_busy 并推进到下一个未来点

#### Scenario: Adapter 空闲但 Global 满
- **WHEN** Adapter 无非终态 Execution但 Global Admission 满
- **THEN** 计划点记录 skipped/admission_full，且不遗留部分 Outbox 或 counter

### Requirement: 每个已处理计划点都有可重建审计结果
Scheduler SHALL 为已跨过的每个 due point保存 `enqueued|coalesced|skipped|expired` 与稳定 reason。连续同结果点 MAY 以 first/last scheduled_for、occurrence count 与 cron/timezone snapshot 聚合，但系统 MUST 能重建精确覆盖集合，且单次处理/记录数量有界。

#### Scenario: 聚合 Coalesced 范围
- **WHEN** 大量连续 due points 都被 coalesce
- **THEN** 系统可保存一个有界范围记录，但验证工具能按冻结 cron/timezone 得到相同 count 与边界

#### Scenario: 审计写入失败
- **WHEN** Execution 创建或 cursor 更新对应的审计结果无法在同一事务提交
- **THEN** 整个计划点事务回滚，不出现有 Execution 无 enqueued 事实或无记录推进 cursor

### Requirement: Scheduler 并发使用数据库时间与唯一约束
多 Control Scheduler SHALL 继续使用数据库时间、Schedule 行锁与 `(adapter_id, scheduled_for)` 唯一约束；policy evaluation、Execution/Admission/Outbox、outcome 与 cursor 更新 MUST 位于一致的受锁事务边界。

#### Scenario: 两个 Scheduler 处理同一 due point
- **WHEN** 两个实例并发轮询同一 Schedule
- **THEN** 最多一个创建或消费该点，另一个观察已推进 cursor/outcome，不产生重复 Execution

#### Scenario: DST 重复本地时间
- **WHEN** timezone/Cron 产生 DST 边界点
- **THEN** 系统沿用现有 UTC scheduled_for 权威与 Cron 规则，policy 不使用浏览器本地时间去重

### Requirement: Schedule UI 与 API 使用同一权威字段
Control API SHALL 返回/接受三个 policy 字段与结构化 outcome；Web MUST 提供 zh-CN/en 文案、范围校验、可聚焦帮助与服务端错误映射。Web 不得自行推进 cursor、推算遗漏结果或把草稿显示为已保存。

#### Scenario: 旧 Web 省略新字段
- **WHEN** 兼容窗口内旧客户端更新其他 Schedule 字段并省略 policy
- **THEN** Control 保持已保存 policy 不变，而不是重置默认

#### Scenario: 双语配置
- **WHEN** 用户在 zh-CN/en 配置三种 policy
- **THEN** 字段值保持稳定英文 enum，显示文案本地化且插值/范围一致
