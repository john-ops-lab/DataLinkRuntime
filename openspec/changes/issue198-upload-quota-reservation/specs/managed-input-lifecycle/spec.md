# Spec Delta

## MODIFIED Requirements

### Requirement: 并发上传使用原子容量预留
每个 `UPLOADING` Artifact MUST 恰好关联一个同 Adapter、唯一、`ACTIVE` 的 reservation；平台占用计算 SHALL 同时计入实际 Blob、pending deletion charge 和有效 ACTIVE reservation，状态迁移不得重复释放容量。系统 SHALL 按当前净字节需求与单文件、Adapter、平台可用容量限制预留；可选批量预留不得使容量内的合法文件被拒绝，单文件上限不得改变配额内文件的可上传性。

#### Scenario: 并发上传竞争最后配额
- **WHEN** 两个上传并发请求会共同突破 Adapter 或平台配额
- **THEN** 最多一个 reservation 原子成功，另一个返回 `adapter_input_quota_exceeded` 或 `platform_input_quota_exceeded`

#### Scenario: Writer 续租
- **WHEN** 活跃上传持续流式写入
- **THEN** writer 按单调时钟的有界周期续租同一 ACTIVE session，而不只在扩容时续租；若 reservation 已过期/取消或续租失败，writer 立即中止并幂等删除 `.part` 或最终 Blob

#### Scenario: 完成上传核销
- **WHEN** Blob 校验并原子落盘完成
- **THEN** 同一数据库事务以实际字节将 `ACTIVE→CONSUMED`、`UPLOADING→STAGED` 并把预留 charge 转为实际占用一次

#### Scenario: 请求取消与完成核销竞争
- **WHEN** Blob rename 后，上传请求取消与数据库完成核销并发
- **THEN** 同一受锁状态机决定补偿方是否拥有文件清理权；核销先成为 `CONSUMED/STAGED` 时正式 Blob MUST 保留，取消先成为 `CANCELLED/DELETED` 时才可清理并释放预留，且不得出现 `STAGED` 但 Blob 缺失

#### Scenario: 恰好填满剩余额度
- **WHEN** 空 Adapter 的剩余额度为 1MiB、单文件上限为 100MiB、平台与磁盘余量充足，上传净 1MiB 文件
- **THEN** 上传成功，净大小/hash 正确，可保存使用，预留全部转换为实际占用

#### Scenario: 多一个字节与大小限制组合
- **WHEN** 文件净大小超过 Adapter 或平台剩余额度 1B，或超过单文件上限
- **THEN** 系统分别返回对应 quota code 或 input_file_too_large，失败预留与部分文件收敛；单文件上限小于、等于、大于剩余额度时均按真实限制判断

#### Scenario: 初始预留大于剩余额度
- **WHEN** 真实剩余额度小于初始预留批量，但合法文件仍可容纳
- **THEN** 系统允许上传该文件；额度耗尽或已有用量超额时仍拒绝新增上传

