## Purpose

定义 Worker 长期依赖缓存的身份、完整性、生命周期和删除安全合同，保证安装、加载、运行、排队、重试及恢复责任受到一致保护，并使旧版本缓存升级后仍能真实命中和执行，未知事实不会被误当成删除许可。

## ADDED Requirements

### Requirement: 生命周期不改变内容完整性

系统 SHALL 将最近使用、固定、重建确认及清理状态与已验证只读缓存内容分离。更新生命周期 MUST 不改变原内容 identity/digest 或放宽完整性校验。

#### Scenario: 更新使用时间及保护状态
- **WHEN** 已有效的 ready 环境更新最近使用时间或 pin
- **THEN** 原 manifest、内容 digest 和只读内容保持不变，同一身份仍通过完整性校验

#### Scenario: 内容被真实篡改
- **WHEN** 已更新生命周期的环境中依赖文件被修改
- **THEN** 内容校验仍失败，不把 sidecar 的历史验证结果视为当前内容证明

#### Scenario: Worker 注册身份变化
- **WHEN** 非空缓存 root 的所属 Worker 与当前注册 ID 不一致，或旧 root 尚无可确认归属
- **THEN** 保留缓存并停用治理，不覆盖 owner 后遗漏旧 Worker 的 queued 引用；仅全新空 root 自动绑定，旧 root 初始化须有升级前身份依据

### Requirement: 旧 ready 缓存兼容且首次观察保守

系统 SHALL 继续识别升级前有效的 ready 缓存并允许同一版本命中；缺失或无法初始化生命周期 MUST 视为回收事实未知，不得视为内容损坏、最早使用时间或删除许可。初始化 SHALL 有界、可重入，失败不破坏内容。

#### Scenario: 保留缓存升级后运行同版本
- **WHEN** 旧版本代码真实准备并成功运行后保留缓存，再用新 Worker 运行同一版本
- **THEN** 原环境继续命中且业务成功，证据包含未发生重新下载/安装、原 manifest/digest 未改变

#### Scenario: 首次扫描或初始化中断
- **WHEN** 旧缓存没有生命周期，或初始化写入中断后重试
- **THEN** 该项保留，成功初始化以当前观察时间为闲置下限，不因缺失时间直接成为删除候选

### Requirement: 本地使用覆盖依赖全生命周期

系统 SHALL 对同一版本以跨进程互斥保护安装、缓存命中检查、加载、运行和终止清理；使用保护 MUST 在依赖副作用前生效，直到 Sandbox 及其子进程确认不再使用缓存。容量预留不能替代使用保护。

#### Scenario: 运行或安装期间触发清理
- **WHEN** 一个进程安装、加载或运行该版本，其他进程请求清理
- **THEN** 清理保留并报告 `cache_in_use`，原 Execution 继续按既有合同运行

#### Scenario: 取消或依赖检查早退
- **WHEN** 执行取消、依赖检查提前返回或进程清理 deferred
- **THEN** 仅在确认 Sandbox 清空后释放使用保护；未确认时持久保护仍存在

#### Scenario: 持久使用记录写入失败
- **WHEN** 仅生命周期 sidecar 写入失败，或必须的 cache-use 记录无法持久化
- **THEN** 前者可继续验证并使用旧 ready 但禁止回收；后者在任何依赖或子进程副作用前拒绝运行，不留下无持久保护的执行分支

### Requirement: 未完成引用由 Control 权威保护

系统 SHALL 保护目标或实际 Worker 上 `queued/running/retry_wait` Execution、非终态 Attempt、open Incident/可恢复材料以及未确认的 Sandbox/Workspace 清理责任。历史终态记录单独存在不要求永久保留。变更当前运行节点、停用或删除 Adapter MUST 不释放仍存在的旧 Worker 责任。

#### Scenario: 旧节点仍有排队或恢复责任
- **WHEN** Adapter 已迁移节点或删除，而旧 Worker 对某版本仍有 queued/retry/恢复责任
- **THEN** 旧 Worker 的对应环境保留，报告具体引用类别

#### Scenario: 逻辑终态但清理或 Incident 未结束
- **WHEN** Execution 已 dead_letter/cancelled，但有 open Incident 或未确认清理责任
- **THEN** 对应缓存仍受保护，不能仅凭逻辑终态删除

### Requirement: 新引用与删除必须串行

所有创建、Replay、重试及人工或自动恢复的新引用 SHALL 与相同 Worker/version 的持久删除门禁原子串行；门禁锁锚点 MUST 在 Adapter 或版本记录消失后仍有效。删除获授权后新引用 SHALL 可重试地暂缓，不产生半成品 Execution、Admission 消耗或错误消费 Schedule 责任。

#### Scenario: 扫描后先出现新引用
- **WHEN** 预览显示可删除，但新引用先于删除门禁提交
- **THEN** 删除保留该项，不使用旧快照授权

#### Scenario: 删除先获得门禁
- **WHEN** 删除已获门禁，随后手动/Schedule/Webhook/Replay/Incident 恢复请求引用同一环境
- **THEN** 新引用收到 `cache_reclamation_in_progress` 或等价既有可重试 claim 决策，只有删除完成或安全撤销后才能重试成功

#### Scenario: 删除后锁锚点仍存在
- **WHEN** 原 Adapter 已永久删除或只剩旧 Worker 缓存
- **THEN** 删除和任何恢复引用仍通过稳定门禁协调，不把缺少业务记录视为自动安全

### Requirement: 不确定状态保留且重启不丢保护

系统 SHALL 将本地使用/删除责任持久化，并检查 Attempt、Workspace 和 Sandbox journal。Control 断联、引用结果不完整、历史字段不明、journal 无法映射或本地进程占用不明时 MUST 保留；时间过期或进程重启不能独立解除保护。

#### Scenario: Worker 崩溃留下进程或旧 journal
- **WHEN** Worker 重启且原内核锁已释放，但仍有旧 use/cleanup/Attempt/Sandbox journal
- **THEN** 先确认对应 Sandbox 和引用事实，不能按 PID 消失或 lease 过期直接删除；无法映射版本时暂停相关缓存树回收

#### Scenario: Control 已授权而本地尚未记录
- **WHEN** acquire 响应丢失或 Worker 在本地删除记录落盘前崩溃
- **THEN** 启动/周期恢复枚举未终结 Control 门禁并与本地记录合并，在同项锁下安全撤销未开始操作或继续有记录的删除，不因本地记录缺失永久遗漏门禁

#### Scenario: 无法联系 Control
- **WHEN** 手动或自动清理不能取得当前 Control 授权
- **THEN** 保留候选并报告 `cache_control_unavailable`，已存在删除门禁也不会自动过期

### Requirement: 删除仅作用于受控对象且可幂等恢复

系统 SHALL 在持有本地排他与当前 Control 授权时重新验证路径、归属、身份和保护状态，持久化删除记录后才移除缓存。删除 MUST 不跟随非授权 symlink、不接受宿主路径、不触及共享或业务数据。失败/中断 SHALL 保留责任并有界恢复，只有实际删除字节才能记为释放。

#### Scenario: 重复操作与回执丢失
- **WHEN** 同一操作重复执行、Worker 中途重启或完成回执丢失
- **THEN** 按同 operation/generation 幂等恢复，不重复扣减容量，不删除同 key 后续重建的新环境

#### Scenario: 路径或所有权异常
- **WHEN** 候选是 symlink、root 本身、非归属目录或身份冲突
- **THEN** 拒绝删除并保留稳定原因，边界外文件不变

#### Scenario: 部分删除或预算耗尽
- **WHEN** 删除发生故障或到达单轮预算
- **THEN** 剩余内容继续计入占用，记录待推进/失败结果，不假报清理成功或释放全部预估空间

### Requirement: 旧清理与修复入口遵守相同安全边界

系统 SHALL 收口旧 stale/Adapter 清理入口，未知 pre-cache 内容不得绕过安全保护；部分保留或失败不得报告整体完成。真实 prepare 中已证明损坏/不可用的环境修复，以及正常 builtin/工具链/选源变化的合法 replacement，SHALL 绑定有效当前 running Attempt、业务 slot、同项排他和持久 use/guard。其他引用只有在尚未领取、同一不可变 version 和 exact builtin snapshot、无历史未确认清理/进程/恢复责任时才可作为未来 claim 保留而不阻止 replacement；不同 builtin snapshot、其他 claimed/running、未知归属或恢复责任 MUST 保护旧实例。普通 GC MUST 继续拒绝任何 queued/retry_wait 引用，不能使用这个 prepare 例外。

#### Scenario: Adapter 清理保留受保护项
- **WHEN** 旧 Adapter 清理任务包含仍被引用的缓存
- **THEN** 返回保留或部分结果，不能吞掉每项失败后标记整个任务 completed

#### Scenario: 当前 Adapter cleanup 不被自身哨兵阻塞
- **WHEN** 治理 Worker 用当前 running cleanup 的正确 claim_attempt 请求清理已永久删除 Adapter 的已验证缓存
- **THEN** 仅排除当前 cleanup 行的哨兵；其他引用、清理、use/journal 和全部策略仍保护，旧领取不能首次授权或完成新领取

#### Scenario: 升级前业务对象已删除且没有 guard
- **WHEN** Worker 在已确认 root 和项锁下完整验证旧 ready 的实际 identity/digest，而原 Adapter/Version 均已不存在
- **THEN** 可用绑定 operation 的 observed_identity 建立稳定锚点，再检查所有保护；不猜身份、不复活业务行、不将仅有 .ready 的 pre-cache 认领为可删除

#### Scenario: Adapter cleanup 预算续作与整体完成
- **WHEN** 扫描/删除预算耗尽、存在 retained/failed、未完成 trash 或 guard 回执
- **THEN** 预算续作保持本次 running；只有完整扫描且所有目标与回执收敛才成功，真实保留/失败按有限尝试停止，不兜底递归删除

#### Scenario: 已领取任务发现损坏环境
- **WHEN** 当前有效 Attempt 独占使用锁，发现同身份环境内容损坏或解释器不可用
- **THEN** 可在有效 Attempt/slot 与持久 guard 下受控修复，其他本地读者与恢复责任仍受保护；未知或错误归属拒绝删除

#### Scenario: 同版本队列遇到合法环境更新
- **WHEN** 多个同版本 queued 或干净 retry_wait 尚未领取，builtin snapshot 与当前 Attempt 相同且没有历史清理/恢复责任
- **THEN** 当前有效 Attempt 可以受控 replacement，其他任务不互相阻塞；新 queued 没有 Attempt 时其初始 cleanup pending 不被误判为旧清理责任

#### Scenario: 后续 claim 的选源发生变化
- **WHEN** replacement 完成后，尚未领取的任务在其 claim 时得到不同默认源、凭据、fallback 或工具链
- **THEN** 继续按已有 prepare 合同计算身份并命中或重建，不强加创建 Execution 时的源快照，也不增加源/凭据全局锁或泄漏 identity 原料

#### Scenario: 其他责任明确需要旧环境
- **WHEN** 其他引用携带不同 builtin snapshot、已经 claimed/running 的旧 payload、未确认历史 cleanup、旧 use/journal 或 open Incident
- **THEN** replacement 保留旧实例并报告具体保护原因；只能排除当前 Attempt/fence 的自身记录，不能忽略同一 Execution 的旧 Attempt

#### Scenario: replacement 准备失败或中断
- **WHEN** 新环境准备失败、当前 Attempt 所有权丢失或旧新实例切换中断
- **THEN** 新 ready 验证发布前旧实例仍受持久记录保护，不假报已释放；按旧新 identity 和原 generation 安全恢复，不继续无授权破坏性操作

### Requirement: 混合版本与业务数据边界

Control SHALL 仅向声明治理能力的 Worker 发删除指令；升级和回滚 MUST 保留原缓存、代码、版本、配置、历史和托管输入。存在未完成删除门禁时不得丢弃对应持久状态或降级其存储结构。

#### Scenario: 新 Control 与旧 Worker 共存
- **WHEN** Worker 尚未声明治理能力
- **THEN** 原执行功能保持兼容，治理入口显示不支持且不发送删除命令

#### Scenario: 同一缓存 root 升级
- **WHEN** 新 Worker 要接管旧 Worker 使用的同一 root
- **THEN** 先停止旧 Worker 并确认旧进程退出，再恢复 journal 和启用新治理；不允许旧 Worker 绕过新锁混用 root

#### Scenario: 回滚存在未完成门禁
- **WHEN** 管理员尝试降级且还有未完成删除
- **THEN** 要求先完成或安全撤销，不清空缓存、不伪造回执、不直接移除门禁表
