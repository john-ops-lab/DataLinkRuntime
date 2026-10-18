# Design

## Context

参见 proposal.md。当前扩容把必需的净累计字节提升到 reservation + 1MiB，仅按单文件上限截断。Adapter 与平台检查因此会把可选预留当成必需容量。

## Goals / Non-Goals

**Goals:** 在原权威事务内使预留增长有界、容量边界可用，保持单次 transport chunk 不打开策略事务。

**Non-Goals:** 不改变配额合同、Multipart 解析、状态机、UI 或持久化结构。

## Decisions

- 所有 writer 保持 Adapter → platform capacity → reservation 的锁序。可用新增字节由 Adapter actual + ACTIVE reserved 和平台 actual + reserved 在锁内计算。
- 区分必需 delta 与理想批量 delta。必需 delta 超额时保留现有准确错误；合法时取理想 delta 与两个余量的最小值。单文件上限仍先校验并截断。初始预留至少要求 1B 可用，并把可选 64KiB 截断到余量，维持配额耗尽/over_quota 时拒绝新增上传。
- 磁盘空间也限制可选增长，必需 delta 仍接受现有低水位校验；stream write 的逐块低水位检查不变。
- 保留 1MiB 批增长，避免直接只预留每个 chunk 而退化成逐块 DB 查询；不取消 reservation 或绕过容量检查。
- 参考 [CephFS quota](https://docs.ceph.com/en/latest/cephfs/quota/) 的按字节容量约束；DLR 继续使用现有 PostgreSQL 强一致账本，而不引入客户端近似配额或新依赖。

## Risks / Trade-offs

- 接近额度边界时可预留批量缩小 → 真正超额仍在写入前拒绝；验证多 1B 和并发。
- 流式磁盘空闲会变化 → 每次实际写入前仍检查低水位。
- 取消与 finalize 竞争 → 保留原状态机与补偿，运行既有 hardening tests。

## Migration Plan

无需数据库迁移；部署新 Backend 即可。回滚恢复原预留算法，不修改已有账本数据。
