## Purpose

定义 Worker v1/v2 兼容、Execution 文件下载授权、三语言 Context 和可恢复 Workspace 清理合同，使文件运行在崩溃、断网和重试下仍安全收敛。

## ADDED Requirements

### Requirement: Worker 注册显式协商协议版本
Worker 注册 SHALL 携带 `protocol_version`；缺失或显式 JSON `null` 按 v1 兼容，支持 Claim/Cleanup Token 与持久清理日志的 Worker 使用 v2。TaskPayload 中非空版本 MUST 是整数 `1` 或 `2`，不得把 bool、float 或数字字符串强制转换为协议版本。Control MUST 保存并在调度判断中使用该版本。

#### Scenario: 旧 Worker 注册
- **WHEN** Worker 注册未携带 protocol_version
- **THEN** Control 将其视为 v1，并只允许领取符合最低协议的 none/json Execution

#### Scenario: v1 领取文件 Execution
- **WHEN** v1 Worker 尝试领取 managed_files Execution
- **THEN** Control 返回 `worker_protocol_incompatible` 且 Execution 保持可由合格 v2 Worker 领取

#### Scenario: 非整数协议版本
- **WHEN** TaskPayload 携带 bool、float、数字字符串或未知整数协议版本
- **THEN** Worker 在 journal、Workspace、依赖准备和 Adapter 进程副作用前返回 `worker_protocol_payload_invalid`

### Requirement: v2 Claim 签发分权 Token
v2 Worker 原子 claim 时 SHALL 获得至少 256-bit CSPRNG 的 Claim Token 和独立 Cleanup Token；Control 只保存各自单向哈希并使用 constant-time comparison。两类 Token MUST 不可互换。

#### Scenario: Progress 使用 Cleanup Token
- **WHEN** Worker 对 progress/result/download 携带 Cleanup Token
- **THEN** Control 返回 `execution_claim_token_invalid`

#### Scenario: 清理回执使用 Claim Token
- **WHEN** Worker 对 cleanup receipt 携带 Claim Token
- **THEN** Control 返回 `execution_cleanup_token_invalid`

#### Scenario: v1 兼容 Execution
- **WHEN** v1 Worker 领取 none/json Execution
- **THEN** Control 不生成 Token，并在终态记录 `deferred/workspace_cleanup_legacy_unverified`

### Requirement: Worker 下载由 Execution、Lease 与 Claim Token 联合授权
内部 Artifact 下载 SHALL 校验 Worker 拥有该 running Execution、Claim Token 有效、Artifact 位于活动 Lease、Artifact 可读且元数据与内容一致；下载 API MUST 不接受浏览器或普通管理员会话替代 Worker 授权。

#### Scenario: Lease 外 Artifact
- **WHEN** 拥有 Execution 的 Worker 猜测同 Adapter 但不在本次 Lease 的 Artifact ID
- **THEN** 下载被拒绝且不返回对象路径或存在性细节

#### Scenario: Token 在 URL
- **WHEN** 请求尝试把 Token 放在 URL/query 而非指定 Header
- **THEN** Control 不接受该凭据，且访问日志不出现 Token

### Requirement: TaskPayload 不暴露存储路径
managed_files TaskPayload SHALL 只包含 Execution 文件项的 ID、ordinal、受控 mount name、展示元数据、大小和 SHA-256，以及 Execution 固化的清理超时；MUST 不包含 Control 宿主路径、storage key 或 ArtifactStore 根路径。受控 mount name SHALL 由 ordinal 与白名单扩展名生成，未知或非法扩展名退化为无扩展名安全名称。

#### Scenario: 领取文件任务
- **WHEN** v2 Worker claim managed_files Execution
- **THEN** 每个文件具有稳定 ordinal 与平台生成的 `input-<ordinal>.<controlled-extension>` 或安全无扩展名 mount name，原始文件名仅为元数据且不参与目录选择

### Requirement: Worker 在启动 Adapter 前完整准备输入
Worker SHALL 在受控 Execution Workspace 内下载全部 Lease 文件，逐项复核 size 与 SHA-256，写入不含敏感路径的 manifest，并将文件设为 `0444`、输入目录设为 `0555` 作为 best-effort 防误写；任一失败 MUST 不启动 Adapter 子进程。

#### Scenario: 下载中断
- **WHEN** 任一 Artifact 下载中断或返回非成功状态
- **THEN** Worker 不启动 Adapter，返回稳定下载错误并进入受控清理

#### Scenario: 校验不匹配
- **WHEN** 下载字节数或 SHA-256 与 TaskPayload 不一致
- **THEN** Worker 删除临时副本、不启动 Adapter并上报 `input_artifact_checksum_mismatch`

#### Scenario: 只读不是安全隔离
- **WHEN** 文档或 UI 解释输入目录权限
- **THEN** 只描述为同 OS 用户下的防误写措施，不宣称强安全只读边界

### Requirement: Workspace 创建前持久化私有清理日志
v2 Worker MUST 在创建 Workspace、下载文件或启动 Adapter 前，把 execution ID、受控计划路径和原始 Cleanup Token 原子写入 Workspace 外的私有 journal；目录权限为 `0700`、文件为 `0600`，写入使用同目录临时文件、fsync 与原子 rename。

#### Scenario: Journal 持久化失败
- **WHEN** Worker 无法安全持久化清理日志
- **THEN** Worker 不创建 Workspace、不下载文件、不启动 Adapter，并上报稳定错误

#### Scenario: Journal 内容审查
- **WHEN** 安全测试读取 journal schema
- **THEN** 只存在 cleanup 所需 execution/path/token，不包含 Claim Token、用户文件名、JSON input、业务 Secret 或 Adapter 输出

#### Scenario: 创建 Workspace 后立即崩溃
- **WHEN** journal 已持久化且 Worker 在 Workspace mkdir 后、完整下载 manifest 写入前崩溃
- **THEN** Workspace 已具有最小归属 marker/manifest，重启扫描可由 journal、受控名称与最小归属事实证明并安全清理，不留下无法收敛的空目录

### Requirement: 同步 Workspace 清理具有硬预算
所有业务终态 SHALL 尝试删除整个 Execution Workspace；单次尝试与总阶段分别受 TaskPayload 快照限制，总预算包含全部尝试、退避和本地确认。达到预算后 MUST 立即以 `deferred` 上报，不得无限阻塞结果。

#### Scenario: 删除文件系统调用挂起
- **WHEN** 单次删除超过 attempt timeout
- **THEN** Worker 中断本次尝试并在总预算内重试或上报 deferred

#### Scenario: 清理失败不覆盖业务成功
- **WHEN** Adapter 输出成功但 Workspace 删除失败
- **THEN** Result 仍为 succeeded，并附 `workspace_cleanup_status=deferred`、`workspace_cleanup_error_code=workspace_cleanup_failed`

### Requirement: Cleanup Receipt 独立且幂等
Worker SHALL 通过 canonical `/api/workers/executions/{execution_id}/workspace-cleanup`、仅接受 Cleanup Token 的独立端点上报 Workspace 清理；端点只允许业务终态的 `deferred→completed` 与 `completed→completed`，MUST 不修改业务状态、output、业务 error、ended_at 或触发重跑。若保留旧路径兼容别名，文档与 Worker client MUST 只使用 canonical 路径。

#### Scenario: 非法 v2 payload
- **WHEN** v2 TaskPayload 缺少 Token、超时不变量、受控 mount name 或其他必需字段
- **THEN** Worker 在创建 journal、Workspace或 Adapter 进程前拒绝 payload并返回稳定协议错误

#### Scenario: v2 代码与依赖字段
- **WHEN** v2 TaskPayload 的 `language` 或 `code` 为空白，或 `requirements` 缺失/不是字符串
- **THEN** Worker 在任何本地副作用前返回 `worker_protocol_payload_invalid`；`requirements=""` 表示无外部依赖并保持合法

#### Scenario: Result 响应丢失但 Workspace 已删除
- **WHEN** Worker 重启后 journal 显示 Workspace 不存在且先前 Result 响应未知
- **THEN** Worker 幂等提交 completed receipt，Control 对 completed→completed 返回成功后 Worker 才删除 journal

#### Scenario: 非法清理状态转换
- **WHEN** Worker 对非终态 Execution 或不允许的状态提交 cleanup completed
- **THEN** Control 返回 `workspace_cleanup_transition_invalid` 且业务事实不变

### Requirement: Worker 重启与周期扫描安全治理孤儿 Workspace
Worker 启动和运行期间 SHALL 扫描 journal 与受控 Workspace；只有同时匹配受控名称、归属标记和 manifest 的 Execution 目录可删除，MUST 不按宽泛年龄/路径删除版本依赖缓存或未知目录。

#### Scenario: Worker 崩溃后恢复
- **WHEN** Worker 在下载、执行或清理阶段崩溃后重启
- **THEN** 它从 Workspace 外 journal 恢复清理，删除遗留副本并幂等回执

#### Scenario: 未知相似目录
- **WHEN** runtime root 中存在名称类似但缺少合法标记或 manifest 的目录
- **THEN** 孤儿扫描保留目录并记录脱敏告警

### Requirement: 三语言 Context 提供稳定文件 API
Python `context.input_files`、JavaScript `context.inputFiles`、Java `context.inputFiles` SHALL 按 ordinal 返回只读元数据对象，至少包含 path、original name、content type、size bytes、SHA-256；JSON `input` 合同继续原样传递。

#### Scenario: Python 读取文件
- **WHEN** Python Adapter 运行 managed_files Execution
- **THEN** `context.input_files` 返回当前 Workspace 的受控路径和快照元数据，`input` 参数为 null

#### Scenario: JavaScript 与 Java 同构合同
- **WHEN** 同一文件输入分别由 JavaScript 和 Java Adapter 执行
- **THEN** 两种语言的 `context.inputFiles` 提供等价顺序与元数据，且不暴露 Control 路径

#### Scenario: JSON Execution
- **WHEN** source 为 json
- **THEN** `handle(context, input)` 继续接收原始 JSON，文件集合为空且不使用包装对象

### Requirement: Token、路径与文件内容不得进入日志
Claim Token MUST 不落盘、不持久化数据库原文、不进入 URL、普通/审计日志、Execution 历史或浏览器；Cleanup Token 只可存在 Worker 私有 journal，不得进入其他日志。错误与审计 SHALL 使用稳定 code 和非敏感 ID。

#### Scenario: 故障注入日志扫描
- **WHEN** 测试注入下载 401、Result 超时、清理失败和 Worker 崩溃
- **THEN** Control/Worker/Web 日志、审计和 API 响应均不包含原始 Token、storage key、宿主路径或用户文件内容
