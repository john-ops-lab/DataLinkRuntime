# Issue #130 Reliable Runtime 迁移说明

本文说明 Batch 1 additive migration 的升级、回滚和旧二进制边界。命令中的
数据库 URL、凭据和宿主机路径均为占位符；不得把真实值写入文档、日志或 shell
历史。

## 迁移路径

`0030_issue130_reliable_runtime` 的父版本是
`0029_issue127_c0_exec_lease`。它只扩展 Schema，不切换 RabbitMQ ingress，也不
实现 v3 Claim/Attempt 运行时。现有 Execution 会确定性回填为
`dispatch_backend=legacy`；`uq_executions_active_adapter`、当前 minimum protocol
和 legacy Claim 在 Batch 1 保留。

部署时使用当前 Control 执行真实 PostgreSQL migration：

```sh
docker compose run --rm control alembic upgrade head
docker compose ps
```

迁移前应保存数据库备份和当前 Alembic revision。独立空数据库与从
`0029_issue127_c0_exec_lease` 建立的 current-main snapshot 都必须执行
`alembic upgrade head`；回归证据位于
`backend/tests/test_migration_m5_4_3.py` 的
`test_fresh_postgresql_upgrade_reaches_issue130_head` 和
`test_current_main_0029_snapshot_upgrades_to_issue130_head_and_backfills_legacy`。

## 非破坏回滚

回滚是部署回滚，不是 Schema downgrade：

1. 关闭 RabbitMQ 新 ingress，保留 Outbox、Admission 和现有 responsibility；
2. 使用仍理解 additive Schema 的兼容 Control drain/repair 已接受责任；
3. 确认 pending/running、Attempt、Slot、Outbox 和计数已按独立审计收敛后，才可
   进行下一步部署决策；
4. 保留新表、快照、审计事实和旧列，不因回滚自动删除数据。

`0026`～`0030` 的 `downgrade()` 仅供隔离测试清理，生产回滚 **禁止** 执行
`alembic downgrade`。任何经单独授权的 reverse migration 都必须先完成备份/恢复
证据、无 active Attempt/Outbox 证明和变更审计；Batch 1 的 pending Outbox 存在时
不得把 downgrade 当作恢复手段。

## 旧二进制 fail-closed 边界

旧 Control/Worker 二进制不能安全解释 RabbitMQ Execution、Outbox 或新的状态并集。
旧版 legacy Claim 只能读取 `dispatch_backend=legacy`；v1/v2 Worker 遇到 RabbitMQ
backend、新状态或不支持的 payload 必须明确拒绝，不能 silent execute，也不能把新
责任改写成 legacy。切换期间应保持当前兼容 Control 运行；不得仅因为旧二进制能连接
数据库就启动它来处理新行。

如果兼容 Control、数据库 revision、pending responsibility 或协议分布无法确认，
部署必须保持 fail closed，停止新增 RabbitMQ responsibility 并升级处理，而不是
删除新表、降低校验或尝试破坏性 downgrade。
