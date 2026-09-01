## Purpose

定义每个 ExecutionAttempt 在 Linux cgroup v2、PID/mount namespace 与有界 tmpfs 中的资源限制、Worker capability preflight、fail-closed 和运行期日志/输出边界，使 OOM、fork、磁盘或日志失控最多终止当前 Attempt，而不拖垮 Worker Agent 与其他平台组件。

## ADDED Requirements

### Requirement: 生产 Attempt 使用固定 Resource Sandbox 合同
RabbitMQ v3 Attempt SHALL 在 Linux cgroup v2、独立 PID/mount namespace 与 bounded tmpfs Workspace 中运行，并 MUST 应用 CPU、Memory/swap、PID、temporary disk、open files、wall-clock timeout、log/spool 与 output 限制。普通未隔离 subprocess 不得作为 v3 生产 fallback。

#### Scenario: Sandbox 成功启动
- **WHEN** Worker 收到具有合法 Resource Profile 的 EXECUTE
- **THEN** Adapter 第一行代码执行前，全部必需 hard limit、namespace、workspace 与进程归属已经验证生效

#### Scenario: Sandbox 准备失败
- **WHEN** 任一 cgroup、namespace、tmpfs 或 limit 无法创建/验证
- **THEN** Adapter 不启动，Attempt 以稳定平台错误收敛并按 Retry Policy 处理

### Requirement: 默认 Resource Profile 有界并固化
系统 SHALL 提供默认 `standard` profile：1.0 CPU core、512 MiB Memory、128 PIDs、1 GiB temporary disk、1024 open files，并沿用 Execution timeout、1 MiB stream 与 512 KiB output 默认值；部署 MAY 在有界范围覆盖。Control MUST 在 Execution 创建时固化 profile，Worker MUST 拒绝缺失、非法或超出声明 capability 的 payload。

#### Scenario: 排队期间修改部署 Profile
- **WHEN** Execution 已 queued 后滚动修改 profile 配置
- **THEN** 既有 Execution 使用原快照，新 Execution 使用新合法值

#### Scenario: Payload 要求超过 Worker capability
- **WHEN** Worker 收到的 memory/pids/disk 等值超出其注册上限
- **THEN** Worker 在 journal、workspace、依赖准备和 Adapter 副作用前 fail closed

### Requirement: Worker 启动 preflight 证明真实能力
Worker v3 SHALL 在启动时用 disposable child cgroup/tmpfs 验证 cgroup v2 delegation、CPU/Memory/PID limit 写入、mount/PID namespace、bounded tmpfs、child kill 与 cleanup residue；注册 MUST 报告每项 capability 和 preflight 结果。静态检查文件存在不得替代真实 probe。

#### Scenario: 完整 Linux capability
- **WHEN** preflight 的创建、超限、kill 与清理断言全部通过
- **THEN** Worker 可声明 `rabbitmq_execution_v3=true` 与完整 isolation matrix

#### Scenario: 非 Linux 或 cgroup 不可写
- **WHEN** 环境不支持 delegated cgroup v2 或 namespace/tmpfs
- **THEN** Worker 可保持诊断健康但必须声明 v3 execution unavailable，Control 不向其切流

#### Scenario: Preflight 留下残留
- **WHEN** disposable cgroup、mount 或 workspace 未能安全清理
- **THEN** capability Gate 失败并记录脱敏 residue ID，不继续创建生产 Sandbox

### Requirement: Worker Agent 保留在 Attempt 资源域之外
Worker Agent、RabbitMQ Consumer、Control client 与 recovery journal SHALL 运行在 Attempt cgroup/namespace 外；平台 MUST 为 Agent 保留资源，所有并发 Attempt 合计不得吞掉 Agent、Control、RabbitMQ 或 PostgreSQL 的最低生存预算。

#### Scenario: Attempt Memory OOM
- **WHEN** Adapter 超过 memory.max
- **THEN** 内核只终止当前 Attempt cgroup 内进程，Worker Agent 继续 heartbeat、回收并处理其他 Adapter

#### Scenario: 所有 Worker slots 同时满载
- **WHEN** 多个不同 Adapter Attempt 同时达到各自 CPU/Memory 限制
- **THEN** Worker Agent 仍可续租、取消、上报与清理，平台健康 Gate 不因资源预算透支失联

### Requirement: PID 与临时磁盘超限只影响当前 Attempt
Sandbox SHALL 以 pids hard limit 防止 fork 失控，并以 bounded tmpfs 对 Workspace/temp 的总字节形成硬上限；不得仅用周期性目录扫描、单文件 `RLIMIT_FSIZE` 或同 UID 权限声称满足总量隔离。

#### Scenario: Fork bomb
- **WHEN** Adapter 持续创建子进程直到 pids.max
- **THEN** 新进程创建失败或当前 Attempt 被终止，其他 Attempt 与 Agent 的 PID 预算不受占用

#### Scenario: 多文件填满 Temporary Disk
- **WHEN** Adapter 用多个文件写满 tmpfs 上限
- **THEN** 写入失败/Attempt 终止并映射 `resource_exceeded_disk`，不能继续占用宿主文件系统

### Requirement: Log、Progress 与 Output 在运行期有界
Worker SHALL 对 `.log`/spool 文件、内存日志缓冲、待上报 progress buffer 与 output read 设置硬字节/条目界限。Output MUST 使用 bounded read/stream，禁止对未知大文件直接无界 `read_bytes()`；达到限制时必须截断或终止并保存稳定事实，不能先耗尽资源再在落库时裁剪。

#### Scenario: 无换行日志洪水
- **WHEN** Adapter 持续输出无换行大块日志
- **THEN** 文件、内存与 pending progress 都保持在上限内，Execution 记录 truncated/limit 事实且 Worker Agent 存活

#### Scenario: 超大 Output 文件
- **WHEN** Adapter 写出远大于 output 上限的文件
- **THEN** Worker 只读取有界 prefix/size 事实并返回稳定 output-too-large，不把整个文件载入内存

### Requirement: Dependency preparation 受同一资源边界
Python/npm/Maven dependency preparation SHALL 在目标 Attempt 的 CPU/Memory/PID/Disk、timeout 与 log limits 内执行，缓存只可通过明确只读/有界合同访问；准备失败 MUST 不绕过 Sandbox 启动普通进程。

#### Scenario: Maven 下载无限输出
- **WHEN** 依赖准备持续输出或超过时间/磁盘限制
- **THEN** 当前 Attempt 受控终止并清理，Worker Agent 与其他语言缓存保持可用

### Requirement: Adapter 拿不到平台隔离控制面
Adapter 进程 MUST 不获得 RabbitMQ Credential、Control Worker Token、Docker socket、宿主 cgroup 写权限、mount capability 或 Sandbox supervisor 控制通道。输入目录权限与 cgroup 资源隔离不得被描述为完整不可信代码安全沙箱。

#### Scenario: Adapter 环境与挂载扫描
- **WHEN** 安全测试从 Python/JavaScript/Java Adapter 检查环境、capability 与 mount
- **THEN** 不存在平台凭据或隔离控制面，且文档仍声明可信管理员代码边界

### Requirement: Resource 超限使用稳定跨层错误
Memory、PIDs、Disk、Timeout 与 Sandbox prepare failure SHALL 映射稳定非本地化 error code，Control/Web MAY 本地化 message 但不得翻译 code；Attempt resource usage/kill reason MUST 可审计且不泄露宿主路径。

#### Scenario: UI 查看 PID 超限
- **WHEN** Attempt 因 pids hard limit 终止
- **THEN** API 返回稳定 `resource_exceeded_pids`，zh-CN/en 显示对应文案并保留相同 code
