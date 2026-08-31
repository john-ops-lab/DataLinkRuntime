## Purpose

定义 Task Adapter 唯一输入对象配置的保存、并发、运行与复制合同，使手动、定时和“立即运行一次”不再维护互相分叉的输入来源。

## ADDED Requirements

### Requirement: 每个 Task Adapter 只有一套当前输入配置
系统 SHALL 为每个 Task Adapter 维护至多一条 Adapter 级输入配置，并以单调递增且不回退的 `revision` 标识每次成功变更；输入配置 MUST 独立于不可变 AdapterVersion。

#### Scenario: 新建 Task Adapter
- **WHEN** 用户创建新的 Task Adapter
- **THEN** 系统创建 `source_type=none` 的当前输入配置并返回初始 revision

#### Scenario: 代码保存不改变输入
- **WHEN** 用户保存新的 AdapterVersion 而未保存输入配置
- **THEN** 输入配置、绑定集合和 revision 保持不变

### Requirement: 输入来源具有封闭类型合同
输入配置 SHALL 支持 `none`、`json`、`managed_files`、`remote_files` 四个枚举值；第一阶段 `none`、`json`、`managed_files` 可保存，`remote_files` MUST 被后端拒绝为 `input_source_not_available`。

#### Scenario: 保存无输入
- **WHEN** 客户端以当前 expected revision 保存 `source_type=none` 且不携带类型专属字段
- **THEN** 系统保存配置、递增 revision，并报告 `valid_for_run=true`

#### Scenario: 保存任意 JSON 顶层值
- **WHEN** 客户端保存 `source_type=json` 并显式携带 object、array、scalar 或 JSON `null`
- **THEN** 系统原样保存该 JSON 值并报告可运行

#### Scenario: 拒绝类型不相容字段
- **WHEN** 请求携带与 `source_type` 不相容的 `json_value`、`artifact_ids` 或 retention 字段
- **THEN** 系统返回 422 校验错误且不改变最近一次有效配置

#### Scenario: 远端文件占位不可保存
- **WHEN** 客户端尝试保存 `source_type=remote_files`
- **THEN** 系统返回稳定 code `input_source_not_available` 且 revision 不变

### Requirement: Managed Files 保存态与运行态分离
系统 SHALL 允许 `managed_files` 保存 0 至 8 个当前 Artifact；只有 1 至 8 个均为当前 Adapter 所有、`READY` 且未过期的 Artifact 时 MUST 报告可运行。

#### Scenario: 保存空文件集合
- **WHEN** 客户端保存 `source_type=managed_files` 和空 `artifact_ids`
- **THEN** 保存成功、revision 递增，并返回 `valid_for_run=false`、`invalid_reason=managed_files_empty`

#### Scenario: Artifact 已到期
- **WHEN** 当前绑定中存在已到期或非 `READY` Artifact
- **THEN** 统一校验返回 `input_invalid` 和结构化 reason，且任何运行入口均不得创建 Execution

### Requirement: 输入配置使用乐观并发控制
所有改变当前输入的用户请求 MUST 携带 `expected_revision`；系统 SHALL 在同一数据库事务中比较 revision、应用完整变更并仅在成功时递增 revision。

#### Scenario: 旧页面提交
- **WHEN** `expected_revision` 不是当前 revision
- **THEN** 系统返回 409 `input_config_revision_conflict`，不修改配置、绑定、Artifact 状态或保留期限

#### Scenario: 保存过程中校验失败
- **WHEN** 权限、Artifact 状态、同名、配额或 retention 校验失败
- **THEN** 最近一次有效配置保持完整可用且 revision 不变

### Requirement: 用户输入写入遵守 Runtime Lock
保存 JSON、切换来源、替换或删除当前文件、修改 retention 的用户写入 MUST 先锁定 Adapter 并在 Schedule enabled 或存在 `pending/running` Execution 时返回 409 `adapter_runtime_locked`；删除未绑定 `STAGED` Artifact不属于当前输入写入。

#### Scenario: Schedule 启用期间修改输入
- **WHEN** Adapter 的 Schedule enabled 且用户尝试保存当前输入
- **THEN** 系统拒绝写入并保持配置与 revision 不变

#### Scenario: 上传不改变运行输入
- **WHEN** Schedule enabled 或存在 active Execution 时用户上传新文件
- **THEN** 上传可停留在 `STAGED`，但绑定、替换和当前输入保存仍被 Runtime Lock 拒绝

#### Scenario: 删除待保存文件
- **WHEN** 有编辑权限的用户删除未绑定 `STAGED` Artifact
- **THEN** 系统接受治理请求且不改变 InputConfig revision

### Requirement: 系统生命周期转换使用完整锁序
会使当前输入失效的到期、损坏或管理员治理 SHALL 通过系统生命周期权限路径执行，并按 `Adapter → Schedule（如涉及）→ AdapterInputConfig → Binding → 按 id 升序的 Artifact → Execution → Lease` 获取所需锁；该路径 MUST 保护活跃 Lease，但不得被用户态 Runtime Lock 永久阻塞。

#### Scenario: 当前绑定文件自动到期
- **WHEN** 系统在完整锁序下确认某个当前 Artifact 到期
- **THEN** 系统原子移除 Binding、递增 revision、将 Artifact 置为 `PENDING_DELETE`，并在集合失效时记录 `input_invalid`

#### Scenario: GC 处理 Blob
- **WHEN** GC 仅处理已经不属于当前绑定的 Artifact Blob
- **THEN** GC 只锁 Artifact 与相关 Lease，且不得反向获取 Adapter 锁

### Requirement: 所有 Task 运行入口复用同一输入解析合同
manual 运行、Scheduler 和 schedule Adapter 的“立即运行一次” SHALL 在同一受锁 Execution 创建流程中读取已保存配置、校验有效性、固定 revision/摘要、创建 Lease 并生成 TaskPayload；长期合同 MUST 不接受 per-run 输入覆盖。

#### Scenario: Manual Adapter 运行一次
- **WHEN** manual Adapter 输入有效且通过现有单活跃门禁
- **THEN** 系统从当前配置创建 `trigger=manual` Execution

#### Scenario: Schedule Adapter 立即运行一次
- **WHEN** schedule Adapter 输入有效且用户点击“立即运行一次”
- **THEN** 系统创建 `trigger=manual` Execution，且不修改 run mode、Schedule enabled、Cron、timezone、`next_run_at` 或其他游标字段

#### Scenario: 输入字段在长期合同下出现
- **WHEN** 兼容开关已关闭且 Execution 请求体出现 `input` 字段，包括 `input:null`
- **THEN** 系统返回 422 `execution_input_override_not_supported` 且不创建 Execution

### Requirement: 运行门禁使用已保存输入
启用 Schedule、manual 运行和 schedule“立即运行一次” MUST 以服务端当前输入有效性为权威，并继续遵守版本、Worker capability/effective-online 和单活跃 Execution 约束。

#### Scenario: 空 managed_files 启用 Schedule
- **WHEN** 用户对空 `managed_files` 输入启用 Schedule
- **THEN** 系统返回 `input_invalid`、`reason=managed_files_empty` 且 Schedule 不启用

#### Scenario: 计划点与立即运行重叠
- **WHEN** schedule“立即运行一次”与 Scheduler 计划点竞争同一 Adapter
- **THEN** 系统沿用单活跃 Execution 门禁，且不引入新的 overlap、misfire 或自动重跑策略

### Requirement: Adapter 复制不共享文件资产
复制 SHALL 深拷贝输入来源、JSON 值和 retention 配置，但 MUST 不复制或复用 Blob、Artifact ID、storage key、Binding、Lease 或文件引用；复制的 Schedule MUST 保持 disabled。

#### Scenario: 复制 JSON Adapter
- **WHEN** 用户复制 `source_type=json` 的 Adapter
- **THEN** 副本获得独立 JSON 值与独立初始 revision

#### Scenario: 复制 Managed Files Adapter
- **WHEN** 用户复制 `source_type=managed_files` 的 Adapter
- **THEN** 副本保留 `managed_files` 和 retention 配置、绑定为空、不可运行，并提示重新上传
