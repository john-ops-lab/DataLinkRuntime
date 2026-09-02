## Context

参见 [proposal.md](./proposal.md) 的 Why 与 Issue #130。当前 main 以 PostgreSQL `executions.status=pending/running/...` 和 Worker HTTP long-poll 形成执行闭环，`uq_executions_active_adapter` 保证每个 Adapter 最多一个 active Execution；Worker protocol 只允许 v1/v2，Agent 本地线程池默认并发 4。#127 已提供 InputConfig、Execution input snapshot、InputArtifact Lease、v2 Claim/Cleanup Token 与 Workspace recovery，但 RabbitMQ、Outbox、Attempt、Retry、Business Dead Letter 和硬资源隔离尚不存在。

当前约束直接影响方案：

- Control 是唯一数据库写入与状态机权威；Worker 不持 PostgreSQL Credential且保持 outbound-only。
- 一个发布必须同时兼容历史 legacy Execution 与新 RabbitMQ Execution，不能把升级窗口当作停机重建。
- #127 的不可变输入、Lease 与 cleanup 事实必须被复用，不能再产生第二套输入生命周期。
- Web 基线固定 React 19、Ant Design 5.29.3、ProComponents 2.8.10；RabbitMQ/资源隔离不得引入第二个 UI 框架或更改这些版本。
- 目标是单服务器、固定 Worker；单节点 Quorum Queue提供功能语义但不提供 Broker HA。
- Resource Sandbox 只承诺资源 containment；可信管理员代码边界不变。

## Goals / Non-Goals

**Goals:**

- 让 202 精确表示 PostgreSQL 已承担执行、重试或可见终态责任；Adapter/Global Admission 与 Outbox backlog 的 count/bytes/age 保持精确，RabbitMQ queue bounds 作为有运维余量的近似第二道保护。
- 用一套 DB 状态机吸收响应丢失、Confirm 歧义、duplicate dispatch、ACK loss、Worker crash 与 Lease expiry。
- 让同一 Adapter 只运行一个 Attempt但可以可靠排队多个 Execution，不阻塞不同 Adapter 使用 Worker slots。
- 保持 legacy v1/v2 可 drain，v3 在 dark launch 与 Sandbox Gate 通过后才最终 Cutover。
- 在真实 Linux cgroup v2 环境证明 CPU/Memory/PID/Disk/Log/Output 失控只影响当前 Attempt。
- 以 B1/B2/B3 串行 checkpoint 和唯一最终 PR交付可审计证据。

**Non-Goals:**

- 不实现 Broker/Control/Relay HA、Worker Pool、跨 Worker reroute 或 Adapter 多槽并发。
- 不提供 Exactly-once；外部副作用仍需 Adapter 使用稳定 execution/idempotency 标识。
- 不把 RabbitMQ Message 变成业务 payload/secret 存储，也不让 Worker直连数据库。
- 不以 Docker socket/sibling container 作为本期 Sandbox控制面，不提供不可信多租户安全承诺。
- 不在 Batch 0/B1/B2 提前删除 legacy schema/path，也不拆成多 PR。

## Decisions

### 1. PostgreSQL 是唯一业务权威，RabbitMQ 是可重建派发层

所有 ingress 进入一个 `accept_execution(...)` 事务边界，职责顺序固定为：

```text
lock Adapter + relevant Input/Artifact rows
validate immutable target/version/input
resolve idempotency
lock adapter admission row
lock global admission singleton
check business + outbox protection
create Execution snapshot
create InputArtifact Lease（如有）
increment admission counters
create generation=1 Outbox
commit
```

RabbitMQ body 只携带定位 Claim 所需 ID；Worker随后从 Control取得不可变 TaskPayload。这样 Broker消息可由 Outbox重建，Secret/Code/Input 不复制到第三个权威存储。

Adapter/Global Admission 与 Outbox backlog 的 count、bytes、oldest age 均从 PostgreSQL 权威事实精确计算并承担业务保护责任；RabbitMQ 的 `messages_ready`、`messages_unacknowledged` 等瞬时计数不能用于确认 202、Admission、Execution 终态或任务是否丢失。

**替代方案：** ingress 直接 DB + RabbitMQ 双写。拒绝，因为任一写入或响应丢失都会留下无法原子解释的半成功。

### 2. Additive 数据模型与约束

#### 2.1 `executions` 扩展

新增/回填：

```text
dispatch_backend             legacy | rabbitmq, non-null after backfill
dispatch_generation          bigint, rabbitmq starts at 1
queued_at / next_attempt_at
attempt_count
max_attempts_snapshot
retry_policy_snapshot        JSONB, validated closed schema
resource_profile_snapshot    JSONB, validated closed schema
target_worker_id_snapshot
logical_input_bytes
idempotency_record_id        nullable
last_error_code
replay_of_execution_id       nullable self reference
```

状态 CheckConstraint 扩展为 legacy/rabbitmq 并集；服务层按 backend校验允许转换。数据库保留 `running` 共享枚举值，但 backend 是非空消歧字段。

#### 2.2 `execution_idempotency_records`

```text
id
adapter_id
key_hash                     bytea(32), raw key never stored
payload_hash                 bytea(32)
execution_id
created_at / expires_at
```

唯一 `(adapter_id,key_hash)`。Cleanup 只选择 `expires_at <= db_now` 且关联 Execution 已终态；Execution retention 在关联记录有效时不得先删。

#### 2.3 Admission counters

```text
adapter_execution_admission(adapter_id PK, outstanding_count, outstanding_bytes, updated_at)
global_execution_admission(singleton_key PK, outstanding_count, outstanding_bytes, updated_at)
```

锁顺序固定 Adapter → Adapter Admission → Global Admission → Execution/Outbox。第一次创建递增；所有终态函数调用一个 `release_admission_once` 条件更新，以 Execution 上的 `admission_released_at` 防重复。Reconciler 比较 counters 与权威状态并在短事务修正。

使用 counter 而非 `SELECT COUNT`，避免并发超卖。Dead Letter进入终态后释放 business outstanding；Artifact Hold是独立治理维度。

#### 2.4 `execution_outbox`

```text
id UUID PK
execution_id FK
dispatch_generation bigint
message_id UUID unique
routing_key
payload_json JSONB
payload_bytes
status pending | published
available_at
lease_owner / lease_expires_at
publish_attempts / last_error_code
published_at / created_at / updated_at
UNIQUE(execution_id,dispatch_generation)
```

Published 行按终态 retention保留足够审计窗口；pending 永不由通用 retention删除。

#### 2.5 Attempt 与 Slot

```text
execution_attempts
  id, execution_id, adapter_id, attempt_no, worker_id
  fencing_token, lease_expires_at
  status claimed|running|succeeded|failed|timed_out|cancelled|worker_lost|resource_exceeded
  claimed_at, started_at, ended_at
  error_code, resource_usage_json, log/output/cleanup summary

adapter_execution_slots
  adapter_id, slot_no (=0 in #130)
  active_attempt_id nullable unique
  fencing_token bigint
  lease_expires_at
  PRIMARY KEY(adapter_id,slot_no)
```

Attempt partial unique index保证一个 Execution最多一个 `claimed/running`；Slot row 保证一个 Adapter只有一个 active Attempt。Claim锁 Execution再锁 Slot，fencing token从 Slot单调递增并复制到 Attempt。#129 可新增 `slot_no>0`，无需改写历史 Attempt。

**替代方案：** 直接在 Attempt.adapter_id 上做永久 partial unique。拒绝，因为 #129 扩多槽时必须重新发明分配权威；显式 Slot更容易迁移和审计。

#### 2.6 Schedule outcome / Infrastructure Incident / Artifact Hold

```text
schedule_dispatch_outcomes
  schedule_id, first_scheduled_for, last_scheduled_for, occurrence_count
  outcome, reason, cron/timezone snapshot, execution_id nullable

execution_infrastructure_incidents
  execution_id, generation, message_id, kind, status, attempts, last_error, timestamps

execution_artifact_holds
  execution_id, artifact_id, reason=dead_letter_replay
  expires_at, purged_at, audit fields
```

Schedule单点 enqueued使用唯一 `(schedule_id,scheduled_for)`/Execution约束；连续同结果的 coalesced/skipped/expired 可聚合，但聚合记录包含足够快照以重建精确点集。Hold不增加 Blob物理 charge，只阻止 GC并单独聚合 held count/bytes。

### 3. Idempotency 使用 JCS 而非原始 Body

Control 在大小/JSON校验后，以 RFC 8785/JCS 规范化值并计算：

```text
payload_hash = SHA256(UTF8(JCS({"trigger": trigger, "body": parsed_body_or_null})))
key_hash     = SHA256(UTF8(exact_header_value))
```

使用一个小型、纯 Python、锁定版本且支持 Python 3.13 的 RFC 8785 实现，并在 Batch 1 先做 compatibility/license Gate。若库不满足，不退化为近似 `json.dumps`；改为仓库内最小 RFC 测试向量实现并重新审计依赖决策。

Hash 不混入当前 Adapter Version/Input/Worker，因为同一个已接受调用在传输重试期间必须返回原 Execution，即使配置已变化。Execution snapshot自行固定这些业务事实。

**替代方案：** hash 原始 JSON bytes。拒绝，因为等价 JSON只改空白/键顺序会错误 conflict。普通 `sort_keys` 也拒绝，因为数字/Unicode不完全满足跨客户端规范化。

### 4. RabbitMQ client 与 topology

引入单一 AMQP runtime dependency `pika>=1.3.2,<2`，通过 uv lock固定实际版本。选择它是因为现有 Control/Worker主要使用同步后台线程，BlockingConnection可以在专用线程中隔离 publish/consume，且直接支持 Confirm/mandatory；不引入 Celery/Kombu及其任务状态模型。

Batch 1 首个依赖 Gate验证 Python 3.13、RabbitMQ 4.3.5、Confirm/mandatory return、connection recovery、TLS与许可证；失败则在任何产品代码依赖它前 `BLOCKED_DEPENDENCY_GATE`，不静默换库。

常量 topology：

```text
exchange dlr.execution.dispatch.v1 (direct,durable)
exchange dlr.execution.infrastructure.dlx (direct,durable)
queue    dlr.worker.<worker_id>.q (quorum,durable,non-auto-delete)
queue    dlr.execution.infrastructure.dlq (quorum,durable)
routing  worker.<worker_id>
```

Queue policy使用 `x-queue-type=quorum`、`reject-publish`、length=2000、bytes=64MiB、有限 delivery limit、at-least-once dead-letter strategy、consumer timeout=300000ms。`max-length`/`max-length-bytes` 是 Broker 的近似排队保护线和最终拒绝条件，不是精确业务容量；RabbitMQ 官方允许受 bounded in-flight publish/delivery 影响出现有界 overshoot，系统不得把它们宣称为精确硬上限。Broker bounds 必须为 Relay 在途窗口和运维处置保留明确 headroom，并通过接近阈值、拒绝 publish、overshoot 与恢复指标/告警暴露；`messages_ready` 仅用于运维观测。Bootstrap用 passive/declare + policy inspection验证实际参数；不兼容漂移使 RabbitMQ capability unhealthy，不删除重建含消息 Queue。

Compose pin `rabbitmq:4.3.5-management` 的实际 digest。管理端口只在隔离开发 profile按需要绑定 localhost，默认服务网络不外露；生产凭据不得使用 guest。

### 5. Outbox Relay 使用 DB lease，Confirm 歧义允许重复

Relay每轮短事务用 `FOR UPDATE SKIP LOCKED`领取 due pending/expired lease，设置随机 owner和短 lease后提交。事务外通过有限数量的 dedicated publisher channels publish persistent/mandatory message并等待 bounded confirm；publisher channel 数、并发 publish 数与 confirm 在途窗口都必须是有限、启动时校验的部署值，不能通过无界内存队列或无界并发绕过 Broker headroom。结果短事务条件更新：

- ack且无 return：`published`；
- 任一 publisher confirm `nack`、mandatory `return`、confirm `timeout` 或 connection loss：保持 Outbox `pending`，增加 attempts，计算 capped exponential backoff；不得删除旧消息、标记 published 或依赖 drop-head 丢弃责任；
- confirm ack后DB标记未知：不推断 exactly-once，lease到期重发。

Outbox oldest/count/bytes来自 DB，是 Ingress `503 outbox_backlog_full` 的精确、权威保护线；Broker `messages_ready` 只作操作指标，不能代替 Admission 或 Outbox backlog，也不能证明业务责任已完成。Relay 必须观测 channel 使用量、并发 publish 数、在途 confirm 数、headroom、reject/return/nack/timeout 与 pending oldest age，并在接近/越过运维阈值时告警。

**替代方案：** 让 RabbitMQ confirm transaction与 PostgreSQL transaction互相等待。拒绝，因为没有分布式事务且会拉长DB锁。

### 6. ACK-on-durable-claim，而不是 ACK-on-terminal

Worker Consumer处理顺序：

```text
acquire local semaphore before delivery work
validate minimal message
POST Control v3 Claim
  DB commit Attempt + Slot + Lease/Fence + Execution running
write/fsync/rename private Attempt journal
basic.ack
prepare sandbox and run
renew lease + progress
report terminal
```

这让 Broker consumer timeout只约束短握手。ACK后 Worker crash由 Attempt lease recovery产生下一 generation；ACK response loss的重投由 active Attempt/generation吸收。

Claim response封闭为：

| Decision | Worker/Broker 动作 |
|---|---|
| EXECUTE | journal成功后 ACK并执行 |
| ACK_NOOP | ACK，释放 semaphore |
| DEFER | RabbitMQ 4.3 delayed retry + bounded jitter，不增加 Attempt |
| REJECT_DLQ | reject(requeue=false)，写 Infrastructure Incident |
| PAUSE_CONSUMER | close/pause channel，服务级 backoff重连 |

Journal失败发生在DB Claim之后，优先调用 `attempt_prepare_failed` terminal接口；若Control不可用，则关闭channel并停止自身执行，等待Lease Recovery。不能以 NACK假装 Claim未发生。

**替代方案：ACK-on-terminal。** 拒绝，因为最长Execution与Broker consumer timeout强耦合，长任务保持大量Unacked，channel recovery语义复杂，DB已有更适合的Lease/Fencing权威。

### 7. Attempt Lease、Fencing 与恢复

默认 lease 60秒、renew 15秒，Control只使用数据库时间。每个 progress/renew/result携带 attempt ID、Claim Token和fencing token；Token数据库只存 hash并constant-time compare。

Reconciler领取过期 Attempt：

```text
lock Attempt -> Execution -> Slot
verify same active_attempt_id/fence and db_now >= lease_expires_at
Attempt -> worker_lost
release Slot conditionally
Execution -> retry_wait or dead_letter
create next Outbox only when retry due, not inside hot recovery loop
commit
```

Worker无法在lease前确认续租时主动终止Sandbox并停止信任本地所有权。网络分区仍可能让旧进程在终止前产生外部副作用，这是at-least-once的已知边界；fence只保护DLR权威状态。

### 8. Retry、Dead Letter 与 Replay

Retry policy作为闭合JSON schema快照：

```text
max_attempts=3
initial_backoff_seconds=5
multiplier=2.0
max_backoff_seconds=300
jitter_ratio=0.2
retryable_error_classes=[platform_transient,worker_lost]
```

`attempt_count`只在成功Claim创建Attempt时增加。Broker DEFER不计。Attempt terminal事务决定：

- succeeded → Execution succeeded；
- cancel → cancelled；
- retryable且有次数 → retry_wait + next_attempt_at；
- 其他 → dead_letter。

Retry Dispatcher只处理 due retry_wait，generation+1并创建Outbox。Replay调用统一 ingress但生成新Execution并引用旧ID；不能把旧dead_letter改回queued。

Managed-files dead-letter transaction创建默认7天Hold。Hold保护线是新managed-files ingress Gate，不得阻止既有Attempt形成终态。Hold到期后Replay返回 `dead_letter_input_expired`，绝不读取当前Binding代替原输入。

### 9. Schedule Policy 与 Cursor

Schema添加 enum与catch-up字段，migration默认 `coalesce_latest/100/86400`。Scheduler在Schedule行锁内计算due点：

- coalesce_latest：只对最新点调用统一 ingress；较早点记录coalesced范围。Admission失败不消费最新责任点，下次重新计算latest。
- queue_every_occurrence：按时间升序逐点accept；第一个Admission失败即停止。超count/age旧点写expired范围。
- skip_while_busy：查询同Adapter rabbitmq非终态；busy或global/outbox full写skipped并推进。

Outcome与Execution/Outbox/cursor在同一事务。为避免一分钟Cron长期停机产生无界行，连续相同outcome聚合为范围，验证器用冻结cron/timezone重建并核对count。

Web保存省略字段时保持已有值，不能用默认覆盖旧客户端请求；新增UI使用项目本地Ant Design 5.29.3 snapshot查询后实现。

### 10. Resource Sandbox 采用 delegated cgroup v2 + tmpfs，不使用 Docker socket

#### 10.1 Host/Compose 前置

Linux部署预先创建只属于Worker的delegated cgroup v2 subtree并以精确bind mount映射到Worker；Worker容器保持 `privileged: false`，仅授予创建私有mount namespace/tmpfs所需的最小 capability。Adapter子进程在启动前drop capability、设置no-new-privileges，且看不到cgroup控制面。任何实际需要扩张capability的实现必须回到design review，不能静默加 `privileged: true` 或挂Docker socket。

macOS/Docker Desktop等不满足真实delegation的环境只运行legacy/单元模拟；正式Sandbox Gate只接受Linux真实probe。

#### 10.2 Attempt supervisor

Worker Agent位于Attempt cgroup外，启动一个短小Supervisor：

1. 创建/配置child cgroup的 `cpu.max`、`memory.max`、`memory.swap.max`、`pids.max`；
2. 在Worker控制的精确Workspace路径挂载size受限tmpfs；
3. Supervisor进入私有mount/PID namespace，只bind自己的Workspace与必要只读runtime/input；
4. 把Supervisor/Adapter进程树加入child cgroup，设置 `RLIMIT_NOFILE` 与wall timeout；
5. 通过有界IPC把start/result/resource事实交给Agent；Agent保留对tmpfs的受控清理权；
6. terminal/timeout/cancel使用 `cgroup.kill`，确认进程为空，卸载tmpfs并删除cgroup；失败写recovery journal由startup scanner重试。

Worker startup preflight用disposable supervisor真实验证创建、CPU/PID/Memory/Disk超限、kill和cleanup。任何步骤失败则注册 `rabbitmq_execution_v3=false`。

#### 10.3 Dependency/cache 与输出

Dependency preparation运行在同一Attempt cgroup/timeout/log边界。既有完整version cache可只读挂载；cache miss在Attempt tmpfs staging中准备，只有通过global cache byte reservation与低水位检查的完整结果才能bounded/atomic promote。缓存管理是平台资源，不得让Adapter直接写共享cache。

`.log`/spool使用预分配/条件写硬上限；内存line buffer和pending progress用固定ring/queue。Output先stat并以 `limit+1` bounded stream读取，记录size/preview/truncated；删除当前无界 `read_bytes()`路径。

**替代方案：**

- 只用RLIMIT或目录轮询：拒绝，不能提供进程树Memory/PID与总临时磁盘硬上限。
- Worker挂Docker socket创建sibling containers：拒绝，Docker socket等价主机控制面且扩大单节点部署/安全复杂度。
- 静默fallback普通subprocess：拒绝，与资源隔离验收相冲突。

### 11. 配置与启动不变量

新增配置按功能分组，所有值有范围并在Control/Worker启动时交叉验证：

```text
DLR_RABBITMQ_EXECUTION_ENABLED=false
DLR_RABBITMQ_URL                         secret, never logged
DLR_RABBITMQ_CONSUMER_TIMEOUT_MS=300000
DLR_RABBITMQ_QUEUE_MAX_LENGTH=2000
DLR_RABBITMQ_QUEUE_MAX_BYTES=67108864

DLR_ADMISSION_ADAPTER_MAX_COUNT=100
DLR_ADMISSION_ADAPTER_MAX_BYTES=1073741824
DLR_ADMISSION_GLOBAL_MAX_COUNT=1000
DLR_ADMISSION_GLOBAL_MAX_BYTES=10737418240
DLR_OUTBOX_MAX_PENDING_COUNT=2000
DLR_OUTBOX_MAX_PENDING_BYTES=67108864
DLR_OUTBOX_MAX_OLDEST_SECONDS=900

DLR_ATTEMPT_LEASE_SECONDS=60
DLR_ATTEMPT_RENEW_SECONDS=15
DLR_IDEMPOTENCY_RETENTION_SECONDS=86400
DLR_DEAD_LETTER_HOLD_SECONDS=604800

DLR_SANDBOX_BACKEND=cgroup_v2
DLR_SANDBOX_CPU_CORES=1.0
DLR_SANDBOX_MEMORY_BYTES=536870912
DLR_SANDBOX_PIDS=128
DLR_SANDBOX_TMP_BYTES=1073741824
DLR_SANDBOX_NOFILE=1024
```

Relay 的 publisher channel 数、最大并发 publish 数与最大 confirm 在途数必须分别配置为正的有限值，并在启动时校验上界、互相关系和 Broker 运维 headroom；不得存在无界 publish task/confirm buffer。其他约束包括 renew < lease/3附近的安全余量、consumer timeout大于Claim+journal预算、adapter limit不大于global、Outbox payload限制不小于单条最小消息、cleanup budget小于recovery grace，以及Resource值大于Supervisor最低开销。

Feature gate只控制新RabbitMQ ingress；Control仍需能读取/恢复已存在RabbitMQ rows，不能用关gate逃避既有责任。

### 12. API 与 Web 兼容

API响应新增字段保持additive；旧legacy状态继续返回。新错误统一为稳定code + structured params，message继续兼容。主要端点：

- 既有Execution ingress接受Idempotency-Key并返回202；
- Worker v3 Claim/renew/start/result/prepare-failed；
- dead-letter Replay与Hold管理员purge；
- Schedule policy/outcome；
- Worker isolation capability与runtime queue health。

普通Execution列表保持轻量，Attempt timeline/Incident/Retry详情只在detail加载。SSE watcher遇到queued/retry_wait先展示状态，进入running后复用日志流；Worker offline不是失败。

Ant Design实现前必须使用仓库 `.agents/skills/antd/SKILL.md` 并查询精确5.29.3 API/token/semantic snapshot。所有新增zh-CN/en keys、插值、键盘可达性与1280/1440/1680/1920视口进入自动Gate。

### 13. 可观测性与安全

指标使用低基数标签：trigger/backend/status/error_class/worker_id；不得把execution/idempotency/token作为metric label。DB权威指标包括Admission、queued/retry/dead-letter、Outbox count/bytes/oldest；Broker指标包括 `messages_ready`/unacked/delayed/redelivery/DLQ、queue bound reject/overshoot 与 Relay channel/concurrency/in-flight/headroom；其中 Broker ready 只作运维指标，不能作为业务正确性依据。Attempt包括lease recovery/resource kill；Migration包括legacy counts/protocol distribution。

日志只记录稳定ID、generation、attempt no/fence摘要和错误码。RabbitMQ URL userinfo、Key原文、Claim/Cleanup Token、Secret、storage key、宿主路径与用户内容进入全局redaction测试。

### 14. AO 与 exact-SHA Gate

AO在同一分支串行运行B1/B2/B3，每批完成后先停止编码并形成checkpoint commit：

```text
Candidate SHA
→ batch-specific tests/fault evidence
→ Sol read-only exact-SHA audit
→ PASS/BLOCKED receipt
```

LOCAL_FAST不触发或声称AO官方Review，不把checkpoint合入main。全部本地Gate通过后才push一个branch并创建一个non-draft REMOTE_RELEASE PR；Hosted CI与AO官方Claude Review必须针对同一head。任何fix/rebase产生新SHA都重跑受影响Gate与exact-head review。

## Risks / Trade-offs

- **[单节点 RabbitMQ 仍是故障点]** → PostgreSQL Outbox保留责任并有背压；明确不声明HA，#129再引入Cluster。
- **[Confirm歧义产生重复消息]** → generation、active Attempt唯一、Slot和Fencing吸收；Adapter外部副作用仍需业务幂等。
- **[Quorum Queue bounds 允许有界 overshoot]** → `reject-publish` 作为最终拒绝第二道保护，DB Admission/Outbox 精确保护线承担业务正确性；Relay 在途窗口有限并保留运维 headroom，通过指标与告警暴露 overshoot/拒绝，不宣称精确 Broker 硬上限。
- **[ACK后Worker崩溃不再有原消息]** → durable Attempt/Lease/journal是恢复权威，Recovery生成新generation；对该路径做故障注入。
- **[同Adapter消息会造成Queue head-of-line]** → prefetch等于slots，Slot busy使用4.3 delayed retry；不引入per-Adapter Queue爆炸。
- **[Admission counter可能漂移]** → terminal条件释放 + 权威Reconciler；所有异常路径测试counter不为负。
- **[JCS/AMQP新增依赖]** → Batch1先验证Python3.13、许可证、API和lock；不引入Celery/Kombu等更大框架。
- **[cgroup delegation与CAP_SYS_ADMIN部署复杂]** → 精确delegated subtree、privileged=false、Adapter drop capability、真实startup preflight；能力不足fail closed。
- **[非Linux本地环境不能证明生产隔离]** → 单元/legacy路径可运行，但发布Gate必须在Linux cgroup v2机器执行并如实标记。
- **[tmpfs与Memory同时计费降低可用容量]** → Resource Profile/Worker reserve按最坏情况计算，Admission不超卖slots；保守默认优先保护Agent。
- **[Dependency cache重新准备成本]** → 只读已验证cache + bounded staging/reservation/atomic promote；不允许绕过Sandbox换性能。
- **[Queue every occurrence制造大量历史]** → catch-up count/age与聚合outcome有界，Business Admission仍是最终保护。
- **[Cutover后无法简单二进制降级]** → additive兼容Control长期保留到drain/invariant通过，明确repair/reverse流程并禁止旧Worker silent execute。
- **[单PR规模较大]** → B1/B2/B3严格串行、每批checkpoint/exact-SHA审计，最终只整合已过Gate的同一分支。

## Migration Plan

### Phase 0 — Batch 0 Design Freeze

1. 同步#127六份delta specs到主规范但保持change active。
2. 更新Issue #130并严格校验本change所有artifact。
3. 不写产品代码；等待用户明确“应用这个 change”。

### Phase 1 — Batch 1 Additive Queue

1. 先做Pika/JCS/RabbitMQ4.3.5兼容与许可证最小实验。
2. 增加RabbitMQ Compose/credential/health/topology，gate默认off。
3. 运行fresh与current-main upgrade migration：状态并集、backend backfill、Outbox/Idempotency/Admission/Attempt/Slot/Schedule/Hold表。
4. 实现统一 ingress、Admission、Idempotency、Outbox Relay和API/Web additive字段。
5. 实现legacy/rabbitmq query硬隔离和inventory/dry-run工具。
6. 在精确checkpoint SHA运行Backend/Web/Rabbit/DB并发与Broker outage/Confirm ambiguity Gate；Sol只读审计。

Rollback：关闭新ingress gate；保留additive schema与Relay恢复能力，legacy流量继续。禁止downgrade删除新表。

### Phase 2 — Batch 2 v3 Dark Launch

1. 实现v3 Consumer、Claim decision、Attempt/Slot/Lease/Fence、journal/ACK。
2. 实现Retry Dispatcher、Business Dead Letter/Replay、Infrastructure DLQ Incident与Hold。
3. 仅为测试/canary创建rabbitmq Execution，运行duplicate、ACK loss、Worker crash、lease expiry、stale fence与migration中断故障注入。
4. 演练legacy pending migrate与running drain，但不切全部新流量。
5. checkpoint exact-SHA Gate/审计；确认minimum、旧索引、legacy Claim均未改变。

Rollback：关闭canary gate，兼容Control继续恢复已经存在的canary rows；生产新流量仍legacy。

### Phase 3 — Batch 3 Sandbox 与 Final Cutover

1. 实现delegated cgroup v2 supervisor、tmpfs、preflight、bounded IO/dependency cache。
2. 在目标Linux环境执行OOM/fork/log/disk/timeout/Agent reserve故障矩阵。
3. 记录DB backup/restore可用性、schema/protocol/legacy inventory与Rabbit readiness。
4. drain legacy running；legacy pending逐条drain或幂等迁移。
5. 所有Worker v3+isolation后切新流量；验证Slot并发；再minimum=3；再drop旧索引；最后legacy清零后关闭Claim。
6. 重跑Post-cutover invariant、full Backend/Web、fresh+upgrade DB、Rabbit故障、三语言Compose、双语浏览器与rollback rehearsal。
7. 形成最终Candidate，push唯一branch并创建唯一PR；Hosted CI与AO官方Review exact-head循环至PASS。

Rollback：Cutover前关gate；Cutover后继续使用兼容Control drain/repair，不启动旧二进制解释新row。任何reverse migration必须先证明无active Attempt/Outbox、制作恢复证据并单独审计。
