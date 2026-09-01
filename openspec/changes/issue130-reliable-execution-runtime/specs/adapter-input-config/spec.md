## MODIFIED Requirements

### Requirement: 用户输入写入遵守 Runtime Lock
保存 JSON、切换来源、替换或删除当前文件、修改 retention 的用户写入 MUST 先锁定 Adapter，并在 Schedule enabled、存在 legacy `pending/running` Execution，或存在 RabbitMQ `running` Execution/active Attempt 时返回 409 `adapter_runtime_locked`；RabbitMQ `queued/retry_wait` 已拥有不可变输入快照，MUST 不因自身存在而扩张用户态 Runtime Lock。删除未绑定 `STAGED` Artifact 不属于当前输入写入。

#### Scenario: Schedule 启用期间修改输入
- **WHEN** Adapter 的 Schedule enabled 且用户尝试保存当前输入
- **THEN** 系统拒绝写入并保持配置与 revision 不变

#### Scenario: 上传不改变运行输入
- **WHEN** Schedule enabled 或存在 active Attempt 时用户上传新文件
- **THEN** 上传可停留在 `STAGED`，但绑定、替换和当前输入保存仍被 Runtime Lock 拒绝

#### Scenario: 删除待保存文件
- **WHEN** 有编辑权限的用户删除未绑定 `STAGED` Artifact
- **THEN** 系统接受治理请求且不改变 InputConfig revision

#### Scenario: 只有 queued 或 retry_wait
- **WHEN** Adapter 没有 legacy active Execution或 active Attempt，但存在已快照的 queued/retry_wait Execution
- **THEN** 用户可按正常 revision/权限合同保存新的当前输入，既有 Execution 快照与 Lease 保持不变

### Requirement: 所有 Task 运行入口复用同一输入解析合同
manual 运行、Scheduler 和 schedule Adapter 的“立即运行一次” SHALL 在同一受锁 Execution 创建流程中读取已保存配置、校验有效性、固定 revision/摘要与具体文件 Lease，并对 RabbitMQ backend 原子创建 Admission/Outbox；TaskPayload MUST 到 Control Claim 时才按不可变快照生成。长期合同 MUST 不接受 per-run 输入覆盖。

#### Scenario: Manual Adapter 运行一次
- **WHEN** manual Adapter 输入有效、固定 Worker 配置合法且 Admission 可用
- **THEN** 系统从当前配置创建 `trigger=manual` 的 queued Execution，即使 Worker 暂时 offline

#### Scenario: Schedule Adapter 立即运行一次
- **WHEN** schedule Adapter 输入有效且用户点击“立即运行一次”
- **THEN** 系统创建 `trigger=manual` Execution，且不修改 run mode、Schedule enabled、Cron、timezone、`next_run_at` 或其他游标字段

#### Scenario: 输入字段在长期合同下出现
- **WHEN** 兼容开关已关闭且 Execution 请求体出现 `input` 字段，包括 `input:null`
- **THEN** 系统返回 422 `execution_input_override_not_supported` 且不创建 Execution

#### Scenario: Ingress 后配置变化
- **WHEN** RabbitMQ Execution 已 queued 后当前 InputConfig 成功保存新 revision
- **THEN** 既有 Execution/Lease继续使用原 revision，新 Execution 使用新 revision，Claim 不重新读取当前配置

### Requirement: 运行门禁使用已保存输入
启用 Schedule、manual 运行和 schedule“立即运行一次” MUST 以服务端当前输入有效性为权威，并继续遵守版本、固定 Worker 存在性与语言/protocol capability，以及统一 Admission 约束。固定 Worker 暂时 effective-offline SHALL 不阻止 queued；legacy backend 继续遵守旧单活跃门禁，RabbitMQ backend MUST 按 Schedule policy 与 Adapter Slot 区分排队和实际并发。

#### Scenario: 空 managed_files 启用 Schedule
- **WHEN** 用户对空 `managed_files` 输入启用 Schedule
- **THEN** 系统返回 `input_invalid`、`reason=managed_files_empty` 且 Schedule 不启用

#### Scenario: 计划点与立即运行重叠
- **WHEN** schedule“立即运行一次”与 Scheduler 计划点竞争同一 Adapter
- **THEN** 两个入口各自使用原子 Admission、planned-point 幂等和已保存 policy；最多一个 active Attempt，但可存在多个合法 queued Execution

#### Scenario: 固定 Worker 暂时离线
- **WHEN** Worker 记录仍存在且 capability 兼容，但 effective-online 为 false
- **THEN** manual/run-now/Schedule 可创建 queued Execution，页面显示等待目标 Worker，不自动 reroute

#### Scenario: 固定 Worker 不存在或不兼容
- **WHEN** runtime_worker_id 为空、目标已删除或 capability 不支持 Adapter language/v3
- **THEN** 系统在 Admission 前返回 `runtime_worker_invalid`，不创建 Execution、Lease 或 Outbox
