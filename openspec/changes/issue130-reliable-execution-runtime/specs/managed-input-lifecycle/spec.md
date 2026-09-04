## MODIFIED Requirements

### Requirement: TTL 与 GC 必须幂等且可重领
上传中断、失败 reservation、UPLOADING 与未绑定 STAGED SHALL 按短 TTL 收敛；GC 核心在领取或删除候选前 MUST 调用不依赖具体 Lease schema 的运行保护 hook，完整系统中的 hook MUST 以 legacy `pending/running`、RabbitMQ `queued/running/retry_wait` Execution Lease，以及未到期 Business Dead Letter Artifact Hold 为权威。GC 只能处理 hook 确认为 unprotected 的候选，并用数据库删除租约支持 stale `DELETING` 被其他实例安全重领。

#### Scenario: Reservation TTL 与完成竞争
- **WHEN** TTL cleaner 与 writer 完成并发
- **THEN** 条件状态更新仅允许一方成功，另一方观察 terminal 状态并幂等补偿，不重复释放预留

#### Scenario: GC 崩溃后重领
- **WHEN** GC 在 `DELETING` 后崩溃且删除租约到期
- **THEN** 其他 GC 可重领并幂等删除；对象已不存在按成功处理

#### Scenario: 删除保护 hook 拒绝候选
- **WHEN** 运行保护 hook 报告 Artifact 为 protected
- **THEN** GC 不迁移 Artifact 状态、不删除 Blob、不释放实际容量 charge

#### Scenario: 活跃 Lease 保护
- **WHEN** Artifact 存在 legacy pending/running 或 RabbitMQ queued/running/retry_wait Execution Lease
- **THEN** GC 不删除 Blob，也不释放实际容量 charge

#### Scenario: Dead Letter Hold 保护
- **WHEN** Artifact 已无 current Binding/active Lease但存在未到期 Dead Letter Hold
- **THEN** GC 保留 Blob 与物理容量 charge，直到 Hold 到期或授权 purge

## ADDED Requirements

### Requirement: Managed File Dead Letter Hold 保证有界 Replay 窗口
Managed-files Execution 进入 Business Dead Letter SHALL 原子创建默认 7 天的 Hold；Hold MUST 关联原 Execution/Artifact、保留 expiry 与审计主体，不重复增加 Blob 物理 byte charge，但 SHALL 单独计 held count/bytes。既有已接受 Execution 即使越过 Hold 告警线也必须成功 dead-letter；保护线达到后 MUST 在 Admission 阶段拒绝新的 managed-files Execution。

#### Scenario: Dead Letter 与当前 Binding 无关
- **WHEN** 原文件已从 Adapter 当前配置移除但对应 Execution 进入 dead_letter
- **THEN** Hold 仍保护 Execution Lease 指向的具体 Artifact，不回退读取当前 Binding

#### Scenario: Hold 保护线达到
- **WHEN** held count/bytes 已达到配置保护线
- **THEN** 新 managed-files ingress 返回稳定容量错误，既有 running Attempt 的终态和 Hold 创建不失败

### Requirement: Hold 到期与 Purge 不改写历史
Hold 到期或授权管理员 purge SHALL 只释放 Blob GC 保护；Execution、Attempt、输入摘要和 dead_letter 原因 MUST 保留。Blob 已删除后 Replay MUST 返回 `dead_letter_input_expired`，不得绑定当前文件或构造伪造 Lease。

#### Scenario: Hold 自然到期
- **WHEN** Hold expiry 已过且没有其他 Binding/Lease/Hold
- **THEN** GC 可按原状态机删除 Blob，历史详情仍显示不可操作摘要

#### Scenario: 管理员提前 Purge
- **WHEN** 管理员对精确 dead-letter Execution 提交 purge 并通过权限/审计
- **THEN** 只释放该 Hold，记录主体、原因、bytes 与结果，不按宽泛年龄删除其他 Artifact
