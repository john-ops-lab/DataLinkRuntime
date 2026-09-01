## MODIFIED Requirements

### Requirement: 服务端 Runtime Lock 是最终权威
Web SHALL 使用 `runtime_locked` 与 InputConfig 响应作为提示，但所有保存、替换、当前文件删除和运行结果 MUST 以服务端 409/422/429/503 为准；disabled 控件通过可聚焦 wrapper 或等价机制提供原因。RabbitMQ queued/retry_wait 本身不得被 Web 推断为输入 Runtime Lock；legacy pending/running、active Attempt 与 enabled Schedule 仍按服务端事实锁定。

#### Scenario: 页面状态过期
- **WHEN** 页面显示可编辑但另一会话刚启用 Schedule或创建 active Attempt
- **THEN** 保存收到 `adapter_runtime_locked` 后页面保持草稿、刷新权威状态并提示解除对应锁

#### Scenario: STAGED 删除
- **WHEN** 当前配置锁定但用户删除未绑定 STAGED 文件
- **THEN** 页面允许删除，且成功后不刷新为新的 input revision

#### Scenario: Schedule 启用期间上传
- **WHEN** Schedule enabled 或存在 active Attempt 且用户选择合法文件
- **THEN** 页面允许上传成为 STAGED 并保留待保存状态，但保存/替换当前 Binding 继续由服务端 Runtime Lock 拒绝

#### Scenario: 只有 queued/retry_wait
- **WHEN** Adapter 没有 active Attempt/legacy active且服务端返回 runtime_locked=false
- **THEN** 页面允许保存新输入并解释既有排队任务仍使用原快照，不因本地状态列表擅自禁用

### Requirement: Schedule Adapter 提供无覆盖的立即运行
schedule 模式 SHALL 显示“立即运行一次”，动作只提交空 Execution body并使用已保存输入；不得提供临时 JSON/文件覆盖控件。成功接收 MUST 展示 queued/running 权威状态，并允许固定 Worker 暂时离线时进入等待。

#### Scenario: 立即运行成功
- **WHEN** 当前输入有效、固定 Worker 配置兼容且 Admission 可用
- **THEN** 页面创建 manual trigger queued Execution并开始现有 watcher/轮询，Schedule cursor 保持不变；Worker offline 时显示等待而非失败

#### Scenario: 输入草稿未保存
- **WHEN** 输入对象存在 dirty 草稿
- **THEN** 页面明确说明运行使用已保存配置或要求先保存，不得把草稿偷偷作为 per-run input 发送

## ADDED Requirements

### Requirement: Web 完整展示可靠队列与 Attempt 状态
Execution 列表、详情、实时 watcher 与 Adapter Header SHALL 支持 `queued/running/retry_wait/succeeded/dead_letter/cancelled/expired`，并继续兼容 legacy `pending/failed/timeout`。详情 MUST 展示 backend、queue/retry 时间、Attempt timeline、稳定错误、是否可 Replay 与基础设施 Incident，但不得暴露 Token、routing key、storage key 或宿主路径。

#### Scenario: Worker Offline 的 Queued Execution
- **WHEN** 固定 Worker 暂时离线且 Execution 已可靠接受
- **THEN** 页面显示“已排队，等待目标 Worker”及创建时间，不显示 adapter_busy或误报失败

#### Scenario: Retry Wait
- **WHEN** Execution 为 retry_wait
- **THEN** 页面显示下一次尝试时间、当前/最大 attempts 与稳定原因，使用服务端时间而非自行修改状态

#### Scenario: 历史 Legacy Timeout
- **WHEN** 页面读取 legacy timeout Execution
- **THEN** 继续显示历史 Timeout，不把它翻译成 RabbitMQ dead_letter

### Requirement: Schedule Policy 表单双语且服务端权威
运行设置 SHALL 提供 coalesce latest、queue every occurrence、skip while busy 三种可聚焦选项，以及有界 catch-up count/age；zh-CN/en key 与插值 MUST 一致。页面只在服务端保存成功后更新权威 policy，并展示最近 `enqueued/coalesced/skipped/expired` 结果。

#### Scenario: Policy 保存 Revision 冲突
- **WHEN** 另一个会话先修改 Schedule 导致服务端 conflict
- **THEN** 页面保留草稿、刷新权威值并要求用户确认，不把草稿显示为已生效

### Requirement: Dead Letter Replay 只在输入仍可用时开放
Web SHALL 根据服务端 `replay_available` 与稳定 reason 显示 Replay；动作 MUST 创建新 Execution并跳转/链接新 ID，旧 dead_letter 保持只读。Managed File Hold 已到期时按钮禁用并显示 `dead_letter_input_expired` 本地化说明。

#### Scenario: Replay 成功
- **WHEN** 用户对可重放 dead_letter 执行 Replay且 Admission 接受
- **THEN** 页面展示新的 queued Execution，旧详情保留 replay relation 与原 Attempt timeline

### Requirement: Isolation Capability 状态不得夸大
系统设置/Worker 状态 SHALL 展示 protocol v3 与 cgroup/memory/pids/tmpfs/bounded-output capability 及 preflight 结果；不支持环境必须显示 fail-closed/不可切流，不得用“Worker 在线”冒充 Resource Sandbox 通过。

#### Scenario: Worker 在线但 Tmpfs 能力缺失
- **WHEN** heartbeat 正常但 isolation matrix 缺少 tmpfs hard limit
- **THEN** 页面显示 v3 execution unavailable与稳定原因，运行入口以服务端 gate 为准

### Requirement: 新增交互遵守既有版本与视口边界
新增 Queue/Schedule/Replay/Capability UI SHALL 使用 React 19、Ant Design 5.29.3 与 ProComponents 2.8.10 的既有项目版本，不引入第二套通用 UI 框架；zh-CN/en 在 1280/1440/1680/1920 宽度 MUST 无关键操作遮挡或横向溢出。

#### Scenario: 双语视口矩阵
- **WHEN** 自动浏览器分别打开队列详情、Schedule policy、Dead Letter 与 Worker capability 页面
- **THEN** 无 raw key、disabled 原因可键盘访问、长错误可控展示且关键动作不溢出
