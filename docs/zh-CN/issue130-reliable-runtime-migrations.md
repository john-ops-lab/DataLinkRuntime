# Issue #130 Reliable Runtime 迁移、Cutover 与故障处理

> 历史设计记录，不是当前部署手册。统一执行运行时已删除 legacy/canary/Cutover
> API、开关和操作路径。不要执行本文历史命令；当前部署见
> [Sandbox 部署与故障定位](issue130-sandbox-deployment.md)。

**简体中文** · [English](../en/issue130-reliable-runtime-migrations.md)

本文是 Reliable Runtime 的部署/API runbook。数据库 URL、凭据、证据 ID 和宿主机
路径都使用占位符；不得把真实值写入文档、命令历史、Issue 或普通日志。Final
Cutover 是显式管理员操作，不会在 `alembic upgrade head` 或普通 Compose 启动时自动
发生。

## Schema 与默认安全状态

`0030_issue130_reliable_runtime` 的父版本是
`0029_issue127_c0_exec_lease`，负责 additive Queue/Outbox/Admission schema；
`0031_issue130_b2_runtime` 再增加 Attempt/Slot/Incident/Hold 与 v3 runtime schema。
历史 Execution 确定性回填为 `dispatch_backend=legacy`。

普通升级只执行 additive migration：

但 additive 不等于无锁或在线无感。`0030` 会回填 `executions` 全表、设置非空约束、
校验多个约束并重建非并发唯一索引；有存量数据的生产库必须安排停止 Execution 写入的
维护窗口，并先按实际表大小验证耗时、锁等待、备份与恢复。不得在持续写入时把下面命令
当作普通滚动升级直接执行。

```sh
docker compose run --rm control alembic upgrade head
docker compose ps
```

升级后默认值保持 fail closed：

```text
DLR_RABBITMQ_EXECUTION_ENABLED=false
DLR_MIN_WORKER_PROTOCOL_VERSION=1
DLR_LEGACY_EXECUTION_CLAIM_ENABLED=true
DLR_CUTOVER_BACKUP_RESTORE_GATE_PASSED=false
DLR_CUTOVER_SANDBOX_GATE_PASSED=false
DLR_CUTOVER_SLOT_GATE_PASSED=false
```

`uq_executions_active_adapter` 的退役是单独、显式、带前置检查的 Cutover 操作，不是
新的 Alembic revision。因而同一 `0031_issue130_b2_runtime` revision 可以处于
“旧索引仍存在”或“已完成 Cutover”两种运维状态；必须通过 inventory/invariant 查询
实际状态，不能只看 revision 猜测。

## 管理员 API

所有端点都要求 `Authorization: Bearer <admin-token>`。下面的主机和 Token 是明显的
占位值：

| 方法与路径 | 是否只读 | 作用 |
|---|---:|---|
| `GET /api/admin/reliable-runtime/inventory` | 是 | revision、backend/status 计数、Worker 协议/Sandbox、Rabbit/Outbox、Cutover gate 与旧索引状态 |
| `POST /api/admin/reliable-runtime/migration/dry-run` | 是 | 计算 legacy pending/running 处理边界，不写数据库 |
| `POST /api/admin/reliable-runtime/migration/legacy-running-drain` | 是 | 断言 legacy running 已清零；从不转换运行中 row |
| `POST /api/admin/reliable-runtime/migration/legacy-pending` | 否 | 以 `limit=1..1000` 逐批幂等转换 pending，并原子创建 Admission/Outbox |
| `GET /api/admin/reliable-runtime/cutover/preflight` | 是 | 分别返回迁移准备 `status/blockers` 与索引退役 `index_retirement` 门禁 |
| `POST /api/admin/reliable-runtime/cutover/retire-legacy-index` | 否 | 在锁与二次检查下退役旧 active index；可安全重跑 |
| `GET /api/admin/reliable-runtime/cutover/invariants` | 是 | 检查 DB 结构不变量、全部 Worker v3/Sandbox 与 Infrastructure DLQ |

只读检查示例：

```sh
curl -fsS \
  -H 'Authorization: Bearer <admin-token>' \
  https://dlr.example.invalid/api/admin/reliable-runtime/inventory

curl -fsS \
  -H 'Authorization: Bearer <admin-token>' \
  https://dlr.example.invalid/api/admin/reliable-runtime/cutover/preflight
```

退役索引必须提交字面确认、preflight 中的准确 schema revision，以及本次已验证的
backup/restore 证据 ID：

```sh
curl -fsS -X POST \
  -H 'Authorization: Bearer <admin-token>' \
  -H 'Content-Type: application/json' \
  --data '{
    "confirmation": "retire-legacy-active-index",
    "expected_schema_revision": "0031_issue130_b2_runtime",
    "backup_restore_evidence_id": "EXAMPLE_RESTORE_EVIDENCE_ID"
  }' \
  https://dlr.example.invalid/api/admin/reliable-runtime/cutover/retire-legacy-index
```

响应 `changed=true` 表示本次删除；同一 schema 下索引已经不存在时，安全重跑返回
`changed=false`。首次删除前若任何 gate、Worker、Rabbit、Outbox、DLQ、legacy active
或结构不变量不满足，调用会返回明确 409，索引保持不变。

## 不可交换的 Final Cutover

每一步失败都停止，不能跳到后一步：

1. 停止部署变更，记录 Candidate SHA/tree、Alembic revision 和 inventory；对当前数据库
   完成真实 backup，并恢复到独立数据库后核对 schema 与关键计数。
2. 在目标 Linux Compose 完成 Worker v3 Sandbox Gate；要求 exact delegated cgroup v2
   subtree、host cgroup namespace 和完整 capability matrix。详细步骤见
   [Sandbox 部署说明](issue130-sandbox-deployment.md)。
3. 调用 dry-run 与 legacy-running-drain。等待 legacy running 按旧合同结束；绝不原地
   转换 running row。
4. 对 legacy pending 选择旧 Worker drain，或分批调用 legacy-pending migrate，直到
   inventory 中 legacy pending/running 都为 0；重跑不得产生重复 Outbox。
5. 确认每个继续服务的 Worker 都是 protocol v3、RabbitMQ capability true、isolation
   preflight passed 且完整矩阵；再开启普通 RabbitMQ ingress。
6. 对 Manual、Schedule、Webhook 和三语言执行做 smoke；确认新 Execution 全部为
   `dispatch_backend=rabbitmq`，legacy Claim 永不读取它们。
7. 在旧索引仍存在时运行 Slot 压力/恢复测试：同 Adapter active Attempt 始终不超过 1，
   不同 Adapter 可并行，终态后 Slot 与 Admission 计数归零。只有通过后才把 minimum
   protocol 提高到 3，并确认 v1/v2 明确拒绝。
8. 设置三个 Cutover attestation，重新调用 preflight；仅在迁移 `status=ready` 且
   `index_retirement.status=ready` 时调用索引退役 API。紧接着重跑同样的 Slot 压力测试。
9. 只有 legacy pending/running 为 0 且旧索引已不存在时，才设置
   `DLR_LEGACY_EXECUTION_CLAIM_ENABLED=false` 并滚动兼容 Control；旧 Claim 返回明确
   `legacy_claim_disabled`，历史终态仍可读取。
10. 连续运行 post-cutover invariant 两次。两次都必须 `status=passed`、
    `violations=[]`，Infrastructure DLQ ready/unacknowledged 均为 0，且结果稳定。

`DLR_CUTOVER_*_GATE_PASSED=true` 是操作员对外部证据的 attestation，不会替代证据
本身。不得在未完成对应实测时设置它们。

## ACK、单节点与外部副作用

v3 正常顺序是：

```text
RabbitMQ delivery
→ Control durable Claim commit
→ Worker 私有 journal 原子落盘
→ ACK
→ Sandbox execute
```

这是 **ACK-on-claim**，不是 ACK-on-completion。ACK 后 Worker 崩溃时，数据库 Attempt
Lease/Fencing 与 Recovery 创建新 generation；系统不依赖原消息仍在 Broker。Publisher
Confirm 丢失可能形成重复 dispatch，但唯一 active Attempt、Slot 与 generation 会吸收
平台内重复。Adapter 对外部系统已经产生的副作用仍必须使用业务幂等键。

默认 Compose 只有一个 RabbitMQ 节点。Quorum Queue 在单节点上提供持久化合同，但
**不提供 HA**。Broker 故障期间 PostgreSQL Outbox 保留已接受责任；Broker 恢复后由
兼容 Control Relay 补发。

## 回滚边界

### Cutover 前

若仍处于 additive/dark-launch、legacy Claim 与旧索引都在，可关闭 RabbitMQ 新 ingress，
保留 additive schema 与 Relay，并让 legacy 新流量继续。先确认已存在的 RabbitMQ
Execution 仍由兼容 Control/Worker drain/repair，不能用关 gate 逃避已接受责任。

### Cutover 后

旧索引退役或 legacy Claim 关闭后，**不能**把启动旧 Control/Worker 当作回滚，也不能
简单关闭 RabbitMQ ingress；后者会把新请求导向已经关闭的 legacy 路径，配置校验会
fail closed。正确恢复方式是：

1. 保持当前 additive schema 与理解 v3 row 的兼容 Control；
2. 在入口侧进入维护/限流，避免新增业务责任，而不是篡改 backend；
3. 修复 Broker/Worker/Control，继续 Relay、Lease Recovery、Retry、Incident/Replay；
4. 反复运行 inventory 与 post-cutover invariant，直到责任和资源收敛；
5. 如确需 reverse migration，另开变更，先证明无 active Attempt/Outbox、完成
   backup/restore 和独立审计。

`0026`～`0031` 的 `downgrade()` 只用于隔离测试清理。生产回滚禁止把
`alembic downgrade` 当作恢复手段。

## 故障处理表

| 现象 | 安全动作 | 禁止动作 |
|---|---|---|
| preflight 为 `blocked` | 保留当前阶段，逐项处理 `blockers`，重新只读检查 | 手工 drop index、伪造 attestation |
| RabbitMQ 不可用 | 保持兼容 Control；观察 Outbox pending 数/字节/最老年龄；恢复同一受控拓扑后确认 Relay 收敛 | 删除 pending Outbox、改成 legacy backend、暴露 management 端口到公网 |
| Outbox 超过保护线 | 在入口侧维护/限流，先恢复 publisher 与 Broker headroom | 扩大无界队列、绕过 Admission |
| Worker offline/崩溃 | 恢复同一固定 v3 Worker；等待 Lease Recovery 与新 generation | 改派到未验证 Worker、启动 v1/v2 解释新 row |
| Infrastructure DLQ 非空 | 查看对应 Incident，修复永久原因后使用受控 Replay，再跑 invariant | 直接清空 DLQ 或删除 Incident 以获得绿灯 |
| invariant 有 violation | 停止后续 Cutover/发布，保存非敏感 sample ID，按具体 code 修复后重跑两次 | 自动改写 row、降低断言、执行 schema downgrade |

所有日志、证据与工单只记录稳定 ID、计数、状态和错误码；不得记录 RabbitMQ URL
userinfo、Claim/Cleanup Token、Credential 真值、storage key、宿主绝对路径或用户内容。

## 旧二进制 fail-closed 边界

旧 Control/Worker 不能安全解释 RabbitMQ Execution、Outbox、新状态或 Attempt/Slot。
legacy Claim 只允许读取 `dispatch_backend=legacy`；v1/v2 Worker 遇到 RabbitMQ backend、
新状态或不支持 payload 必须明确拒绝，不能 silent execute 或把责任改写成 legacy。

如果兼容 Control、数据库实际状态、pending responsibility、Worker 协议或 Sandbox
证据无法确认，部署保持 fail closed：不做下一步变更，并升级处理，而不是删除新表、
降低校验或伪造完成状态。
