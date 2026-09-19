## Purpose

保证已经部署的 Worker 数据库约束与 ORM 声明保持一致，避免后续 schema diff 误判已有能力约束、重复创建索引或产生反向迁移，同时保留已有数据和迁移历史。

## ADDED Requirements

### Requirement: Worker metadata 与已发布数据库对象一致
系统 SHALL 声明与已发布 `0031_issue130_b2_runtime` 完全一致的 `ck_workers_isolation_preflight_status` 和 `ix_workers_rabbitmq_execution_v3`；约束表达式、名称与索引字段顺序 MUST 一致，不得新增重复迁移、重复对象或删除已有对象。

#### Scenario: 真实迁移库与声明比较
- **WHEN** 从空库完整迁移及从既有基线升级后核对 Worker schema
- **THEN** Check 表达式为 `isolation_preflight_status IN ('unknown', 'passed', 'failed')`，Index 字段依次为 protocol_version、rabbitmq_execution_v3，metadata diff 不再报告这两个对象缺失

#### Scenario: 保留原有对象
- **WHEN** 本检查点应用于已含上述对象的数据库
- **THEN** 对象不被重建、重命名或重复创建，Worker 数据保持不变
