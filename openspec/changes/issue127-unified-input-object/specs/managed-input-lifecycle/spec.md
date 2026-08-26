## Purpose

定义 Adapter 当前文件输入从容量预留、流式上传、原子绑定到到期、删除和 GC 的完整治理合同，确保并发、配额、故障与权限场景均可收敛。

## ADDED Requirements

### Requirement: Managed Input 设置具有单一权威来源
系统 SHALL 以数据库单例和管理员 API 管理默认保留期、单文件/Adapter/平台配额、手动删除许可、自定义保留上限、磁盘低水位和 STAGED TTL；所有值 MUST 非空、有界，只有管理员可写。ArtifactStore 根路径、feature flag、后台任务间隔和 Worker 协议/清理超时 MUST 仅由部署环境变量提供。

#### Scenario: 管理员读取策略与用量
- **WHEN** 管理员调用 `GET /api/system/managed-input-settings`
- **THEN** 响应返回规范化设置、平台与 Adapter 用量、配置上限及 `over_quota`，且不返回部署路径或 Token

#### Scenario: 非管理员修改设置
- **WHEN** 非管理员调用设置更新 API
- **THEN** 系统拒绝请求且现有策略不变

#### Scenario: 降低配额低于现有用量
- **WHEN** 管理员把平台或 Adapter 配额降到当前实际占用以下
- **THEN** 现有文件与 Execution 保持可读，范围进入可观测 `over_quota`，新增上传和替换被拒绝，减少占用的删除与 GC 继续允许

### Requirement: 文件类型、名称和大小由服务端权威校验
第一阶段服务端 SHALL 至少允许 `.xlsx`、`.xls`、`.csv`、`.log`、`.txt`、`.json`，MUST 拒绝压缩包；扩展名白名单、流式字节数、配额和磁盘低水位是权威事实，浏览器 accept 与 MIME 仅作提示/元数据。

#### Scenario: 不可信 MIME
- **WHEN** MIME 与文件扩展名不一致但扩展名在白名单且其他校验通过
- **THEN** 系统记录 MIME 元数据但不把它当作内容真实性证明

#### Scenario: 不允许的扩展名
- **WHEN** 上传文件扩展名不在固定白名单
- **THEN** 系统返回 `input_file_type_not_allowed` 并清理预留与部分对象

#### Scenario: 流式大小超限
- **WHEN** 实际写入字节超过单文件限制或预留无法原子扩容
- **THEN** writer 立即停止并返回 `input_file_too_large` 或对应 quota code，不能先写满磁盘再补记账

### Requirement: 并发上传使用原子容量预留
每个 `UPLOADING` Artifact MUST 恰好关联一个同 Adapter、唯一、`ACTIVE` 的 reservation；平台占用计算 SHALL 同时计入实际 Blob、pending deletion charge 和有效 ACTIVE reservation，状态迁移不得重复释放容量。

#### Scenario: 并发上传竞争最后配额
- **WHEN** 两个上传并发请求会共同突破 Adapter 或平台配额
- **THEN** 最多一个 reservation 原子成功，另一个返回 `adapter_input_quota_exceeded` 或 `platform_input_quota_exceeded`

#### Scenario: Writer 续租
- **WHEN** 活跃上传持续流式写入
- **THEN** writer 只续租同一 ACTIVE session；若 reservation 已过期/取消，writer 立即中止并幂等删除 `.part` 或最终 Blob

#### Scenario: 完成上传核销
- **WHEN** Blob 校验并原子落盘完成
- **THEN** 同一数据库事务以实际字节将 `ACTIVE→CONSUMED`、`UPLOADING→STAGED` 并把预留 charge 转为实际占用一次

### Requirement: ArtifactStore 保证受控、原子且不可猜测的对象路径
LocalFileArtifactStore SHALL 使用随机不透明 `storage_key`，原始文件名不得参与真实路径；`.part` 与最终对象 MUST 位于同一支持原子 rename 的文件系统，并防御路径穿越与符号链接。

#### Scenario: 上传成功顺序
- **WHEN** 流式大小与 SHA-256 校验成功
- **THEN** 系统先原子 rename `.part` 为最终随机对象，再允许数据库 Artifact 成为 `STAGED`

#### Scenario: 数据库提交失败
- **WHEN** Blob 已 rename 但 `STAGED` 事务失败
- **THEN** 补偿流程幂等删除该随机对象，低频审计只治理超过安全宽限且无 Artifact/删除任务记录的对象

#### Scenario: 原始文件名攻击
- **WHEN** 文件名包含 `..`、路径分隔符、大小写混淆或 Unicode 等价序列
- **THEN** 存储路径仍只由随机 key 决定，展示名按 Unicode NFC 与大小写折叠参与当前集合冲突检查

### Requirement: Artifact 与 Binding 具有可验证状态机
Artifact SHALL 使用 `UPLOADING → STAGED → READY → PENDING_DELETE → DELETING → DELETED` 主路径，删除失败进入可重试 `DELETE_FAILED`；Binding MUST 只表达当前 revision 的有序选择并强制同 Adapter 所有权、0 至 7 ordinal 与唯一约束。

#### Scenario: 页面刷新恢复待保存文件
- **WHEN** 有编辑权限的用户调用 `GET /api/adapters/{adapter_id}/input-artifacts?status=staged`
- **THEN** 系统仅返回该 Adapter 可管理的 STAGED 元数据，不返回 storage key 或路径

#### Scenario: 跨 Adapter 猜测绑定
- **WHEN** 用户把其他 Adapter 的 Artifact ID 提交到当前配置
- **THEN** 系统拒绝请求，且不泄露该 Artifact 是否存在

#### Scenario: 显式替换
- **WHEN** 用户替换当前文件
- **THEN** 新文件先成为独立 STAGED Artifact，随后一次原子配置保存切换 Binding；旧 Blob 永不原地覆盖

### Requirement: 保存绑定原子具体化 retention
保存 `managed_files` SHALL 在一个受锁事务中校验权限/所有权/状态/数量/同名/配额，替换 Binding、递增 revision、将新绑定 STAGED 置为 READY、具体化 `retention_mode/expires_at`，并将移除的旧 Artifact 置为 `PENDING_DELETE`。

#### Scenario: 使用系统默认保留期
- **WHEN** 保存时选择 `system_default`
- **THEN** 服务端以当时数据库设置计算并返回具体 `expires_at`，后续管理员改默认值不追溯重算

#### Scenario: 修改当前集合保留策略
- **WHEN** 用户在同一配置 revision 上重新保存 retention
- **THEN** 当前绑定 Artifact 在同一事务中重算；已移除 Artifact 保留原具体策略并进入治理

#### Scenario: 永久保留
- **WHEN** 策略为 `manual_delete` 且管理员设置允许
- **THEN** Artifact 不按时间到期，但用户/管理员/Adapter 删除与损坏治理仍可删除它

### Requirement: 磁盘低水位与容量记账覆盖所有占用阶段
系统 SHALL 在写入前和流式写入期间检查最小剩余空间；`STAGED`、`READY`、`PENDING_DELETE`、`DELETING`、`DELETE_FAILED` Blob 均 MUST 占用容量，只有确认 Blob 删除或不存在后才能释放实际 charge。

#### Scenario: 低水位拒绝
- **WHEN** 新写入会使 ArtifactStore 可用空间低于配置下限
- **THEN** 系统返回 `artifact_store_low_watermark`，清理上传副作用且不自动删除现有文件

#### Scenario: 删除重试
- **WHEN** Blob 删除第一次失败
- **THEN** Artifact 进入 `DELETE_FAILED` 并保留容量 charge、错误码、尝试次数与可观测告警

### Requirement: TTL 与 GC 必须幂等且可重领
上传中断、失败 reservation、UPLOADING 与未绑定 STAGED SHALL 按短 TTL 收敛；GC 只能处理无 pending/running Execution Lease 的候选，并用数据库删除租约支持 stale `DELETING` 被其他实例安全重领。

#### Scenario: Reservation TTL 与完成竞争
- **WHEN** TTL cleaner 与 writer 完成并发
- **THEN** 条件状态更新仅允许一方成功，另一方观察 terminal 状态并幂等补偿，不重复释放预留

#### Scenario: GC 崩溃后重领
- **WHEN** GC 在 `DELETING` 后崩溃且删除租约到期
- **THEN** 其他 GC 可重领并幂等删除；对象已不存在按成功处理

#### Scenario: 活跃 Lease 保护
- **WHEN** Artifact 存在 pending/running Execution Lease
- **THEN** GC 不删除 Blob，也不释放实际容量 charge

### Requirement: Adapter 删除与上传创建串行化
上传会话创建与 Adapter 删除 MUST 都先锁定 Adapter 行；存在 `UPLOADING` Artifact 或 ACTIVE reservation 时删除 SHALL 返回 409 `adapter_upload_in_progress`。删除事务 MUST 在移除 Artifact 元数据前为每个已计费未删除 Blob创建独立 deletion job 并原子移交平台 charge。

#### Scenario: 删除与新上传竞争
- **WHEN** Adapter 删除和上传会话创建并发
- **THEN** 上传要么先建立 reservation 并阻止删除，要么在删除提交后因 Adapter 不存在被拒绝，不产生孤儿 writer

#### Scenario: 删除任务释放容量
- **WHEN** deletion job 确认 Blob 已删除或不存在
- **THEN** 仅在 `capacity_released_at` 为空时原子扣减一次平台占用并完成任务

#### Scenario: 删除任务不依赖 Adapter
- **WHEN** Adapter、Binding、Artifact 和 terminal reservation 元数据已删除
- **THEN** deletion job 仍保留 former adapter 审计快照并可独立重试，不持有 Adapter 外键

### Requirement: Managed Input 操作遵守权限、审计与脱敏合同
上传、列出、绑定、替换、删除 MUST 要求所属 Adapter 编辑权限；管理员治理使用独立授权路径。系统 SHALL 审计上传、绑定、替换、删除和管理员治理的主体、Adapter、Artifact、结果与稳定 code，但 MUST 不记录文件内容、storage key、Token 或宿主路径。

#### Scenario: 同 Adapter 协作收尾
- **WHEN** 一个有编辑权限的用户查看另一位编辑者上传的 STAGED Artifact
- **THEN** 用户可为同一 Adapter 绑定或删除它，created_by 和 upload_session 仅用于审计与刷新恢复

#### Scenario: 审计失败操作
- **WHEN** 上传、绑定或治理失败
- **THEN** 审计记录稳定错误码和非敏感标识，普通日志与审计日志不包含用户文件内容或内部路径
