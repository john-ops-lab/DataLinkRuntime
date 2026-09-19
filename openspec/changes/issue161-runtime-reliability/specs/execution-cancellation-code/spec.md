## Purpose

让正常 RabbitMQ 取消通过不同并发路径收敛时，对 API、页面及自动化暴露同一稳定机器码，同时保留既有取消状态、终态不可重开和资源只能释放一次的合同。

## ADDED Requirements

### Requirement: 正常取消使用唯一机器码
新形成的正常取消 Execution SHALL 为 `status=cancelled`、`error_code=execution_cancelled`、`last_error_code=execution_cancelled`；以取消收敛的 Attempt MUST 使用相同 canonical code。该要求覆盖 queued、retry_wait、claim 前 cancel flag 和运行中协作取消。系统 MUST 不批量改写历史终态或改变 decision reason/status 的含义。

#### Scenario: 未 Claim 前取消
- **WHEN** queued Execution 被取消且消息仍在 Broker
- **THEN** API 与页面显示 canonical code，后续 Claim 为 ACK_NOOP 且没有新 Attempt

#### Scenario: retry_wait 和重复请求
- **WHEN** retry_wait 被取消或对已取消结果重复请求
- **THEN** 新取消使用 canonical code，重复请求返回原结果，不产生新派发或第二次释放

#### Scenario: Claim 观察到取消标志
- **WHEN** Claim 在受锁资格检查中发现 cancel_requested
- **THEN** 取消终态使用相同 error_code 与 last_error_code，并保留原取消释放规则

#### Scenario: Running 与 Result 竞争
- **WHEN** 取消、Claim、Result 或 Lease Recovery 并发且合法取消路径先取得权威
- **THEN** 该取消终态仍使用 canonical code，Slot、Admission、Input Lease 只按既有状态机释放一次，旧 fence 不覆盖新状态

### Requirement: 终态历史不可因统一码重写
系统 SHALL 对已终态 Execution 保持幂等观察，不以本次字段统一重开或修改历史 Attempt、错误码、cleanup 责任。

#### Scenario: 已成功或历史旧码记录再次取消
- **WHEN** 请求目标已成功或已以历史取消码终态
- **THEN** 返回既有终态，历史字段保持不变且没有新的资源释放
