## Context

动机与交付范围见 [proposal.md](proposal.md)。代码核对基线为 `e5113bb3b359651fa592925c716a354c0c4ac9cb`；以下均为规划，不代表运行验收已通过。

| 当前实现 | 需要修复的边界 |
| --- | --- |
| `models/execution.py:Worker` 缺少 Alembic `0031_issue130_b2_runtime` 的 isolation Check / capability Index | 仅补 metadata，不重新创建已存在对象 |
| `execution_cancellation.request_cancellation()` 写 `last_error_code=cancelled`，`attempt.claim_dispatch()` 的 cancel flag 分支未写 canonical error，`_apply_terminal_locked()` 已写 `execution_cancelled` | 统一三条终态路径的机器码 |
| `attempt.recover_expired_attempts()` 按最早 lease 排序，失败 rollback 后立即 raise | 坏行反复占首批；同 tick 后续 retry / hold 不运行 |
| `worker/consumer.py` 一个 consumer，prefetch=S；ACK 释放 Broker credit，但本地槽直到执行完成才释放 | 满槽收到消息时主动关闭连接；正常容量演变为失败重投 |
| `_prepare_execute()` 先在同一 pool 提交 `_run_attempt`，再安排 ACK 回调 | 消除执行先于 ACK 回调的竞态，并避免嵌套提交 |
| `infrastructure_dlq.reconcile_message()` 保存 Incident，delivery-limit 留人工审查；`_has_delivery_limit()` 把任意非空 x-death 当 delivery-limit | 需要现存 Incident 入口和准确分类；非 delivery-limit 自动流程保留 |
| `execution.cancel_execution()` 内部 commit；`replay_execution()` 创建新 Execution | 前者抽出事务内 helper，后者不能用于 Incident 恢复 |
| `tools/local-preview/deploy.sh` 将所有 queued/retry_wait 或 cleanup=pending 视为 busy | 永久 queued Incident 可能阻止修复版本升级；入口初始化的 pending 不一定代表真实工作区 |

原合同来源：`openspec/changes/issue130-reliable-execution-runtime/specs/{rabbitmq-dispatch-outbox,execution-attempt-lifecycle,execution-retry-dead-letter,execution-input-snapshot}/spec.md`。保留全部既有验收，特别是 durable Claim/journal 后 ACK、默认 300000 ms consumer timeout 只覆盖握手、24 小时业务不留 unacked、同 Adapter Slot 0、真实 Linux Sandbox 以及新 generation fencing。

## Goals / Non-Goals

**Goals:** 所有已接受责任可继续或通过现有状态机合法终结；扫描/接收/处置均有界；重复消息、并发人工操作、进程重启不制造额外业务执行或资源释放；旧 Incident 和冻结材料在升级后可验证。

**Non-Goals:** 不增加自动 delivery-limit 恢复、不自动替换输入或目标 Worker、不增加业务重试额度、不改变外部副作用幂等责任、不改变业务状态枚举或放宽 Broker 上限。设计与独立 Review 使用指定 Astra ultra，实施 Worker 使用指定 Sol medium；内部 LOCAL_FAST 不操作远端，最终 REMOTE_RELEASE 由集成 owner 执行。

## Decisions

### 1. 先做独立的小型一致性修复

#135 A 在 Worker metadata 加入 `ck_workers_isolation_preflight_status`，表达式精确为 `isolation_preflight_status IN ('unknown', 'passed', 'failed')`；加入 `ix_workers_rabbitmq_execution_v3(protocol_version, rabbitmq_execution_v3)`。以真实迁移库的 `pg_constraint`、`pg_indexes` 与 SQLAlchemy inspect 核对，不生成重复 migration。

#135 B 正常 RabbitMQ 取消终态的 `Execution.error_code` 与 `last_error_code` 均为 `execution_cancelled`；纯 cancel Attempt 的 `error_code` 同值。若先前 Attempt 已经合法以其他原因终态，取消不重写它的历史。共享无提交的 terminal-code helper，覆盖 queued/retry_wait、claim 前 cancel_requested 及 active 协作取消；`cancelled` 仍是状态/decision reason。历史终态只读返回，不批量改写。现有 Admission、Slot、Lease、Outbox、cleanup 与 fence 更新顺序不变。

### 2. 持久 keyset 游标提供跨批次公平，单行错误使用封闭分类

新增 `runtime_reconciliation_cursors`：`name varchar(64)` 主键、`after_id bigint NOT NULL DEFAULT 0`、`upper_id bigint NOT NULL DEFAULT 0`、`updated_at timestamptz`；约束 `0 <= after_id <= upper_id`。迁移种子仅 `expired_attempts` 一行，并加 `execution_attempts(id)` 的 active partial index（`status IN ('claimed','running')`）。这是扫描进度，不是业务 lease、重试时间或终态。

`reserve_recovery_candidates(session, limit)` 在短事务锁游标行，以 `B=max(1,min(limit,1000))` 读取本轮 `after_id < id <= upper_id` 的最多 B 个 active Attempt ID，按 ID 递增；扫描集合包含未过期 active 项，过期资格在处理时重检，避免为了填满批次无限扫描。轮次起点固定 `upper_id=max(active Attempt.id)`，新到 Attempt 留下一轮；无后继时仅允许一次回绕并固定新的 upper_id。最多两次有界候选查询，不补满处理失败项。先提交候选高水位，再逐项处理；不持游标锁获取 Adapter/Execution 锁。

每个候选使用独立短事务或显式 rollback 后已清空状态的 Session，经现有 `_lock_attempt_context()` 锁顺序重读 Attempt/Execution/Slot，再查数据库时间和 lease。租约延长、已终态、已删除或另一个恢复者先完成是 no-op。成功仍调用原 `_apply_terminal_locked()`，不发明第二套恢复终态。

只隔离显式的 `ReconciliationRowError(code)`：由冻结 retry/resource snapshot 的确定性校验转出；初始白名单为 `retry_policy_invalid`、`resource_profile_invalid`，新增类别必须有精确产生点与测试。现有 `HTTPException` 仅在已知 validator 边界按这两个 code 转换，不能捕获所有 4xx。正常并发导致的 not-found 在身份重读后 no-op。`IntegrityError/OperationalError/DBAPIError`、Admission drift、未知 `RuntimeError/ValueError/TypeError` 一律 rollback 后上抛，由既有 loop 报错；禁止 broad except-continue。

隔离错误仅记录 attempt/execution ID、固定 error code、cursor cycle 和计数，不记录异常正文、SQL 参数、输入或凭据。行级错误不阻止当前批后继及本 tick 的 `retry_dispatcher_once()` / `expire_holds()`；系统性错误仍使 tick 失败，不能伪装 PASS。

公平界：有限一轮 C 个 active 候选、每次 B 个，在未持续锁阻塞/DB 故障且有完成的 tick 时，某个持续过期项最多跨当前余轮和下一完整轮获得机会，上界为 `2 * (ceil(C / B) + 1)` 次候选保留（按两轮实际 C 取最大值）。新建更高 ID 不无限延长当轮。并发实例通过短游标锁保留不同段；轮次交叠/崩溃后可能重看同 ID，由业务锁保证幂等。进程在保留后崩溃会跳过该段至下一轮，不丢永久责任；游标重启不重置。永久不释放的业务锁不承诺有限墙钟完成，配置短事务 lock timeout 并公开失败，不能把它吞成坏行。

拒绝方案：只 rollback/continue 无跨批次前进；内存游标重启会重饿死；改 lease 或假终态破坏业务；无限坏行缓存和无界扫描违反有界性。

### 3. 每个真实执行槽对应一次性接收信用

保留 Pika `1.3.2`，Worker 接收端使用其 `SelectConnection`；Control Relay 不改。每个预留槽建立一个 `auto_ack=False`、`prefetch_count=1, global_qos=False` 的 consumer。收到第一条消息后，先异步 `basic_cancel(tag)`，收到 `CancelOk` 后才提交 Claim 工作；该消息在整个 cancel 期间仍未 ACK/NACK，所以该 tag 没有第二条信用。其他 tag 的在途消息各自已有槽预留。取消订阅不是释放执行槽。

最小 `SlotTicket` 持有 `slot_id / connection_epoch / consumer_tag / delivery_tag / phase`；phase 为 `receiving`（含注册）、`cancelling`、`working`、`released`。工作内部 journal/ACK 边界用明确 Future/Event receipt，不再创建另一条 pool 排队任务。一次 ticket 从注册至 Attempt 退出只释放一次。

```text
free → reserve ticket → consume(prefetch=1)
  → delivery → cancel(tag) → CancelOk
  → pool: Claim commit → validate → durable journal
  → IO: basic_ack(original epoch/channel/tag) → ACK-send receipt
  → 同一 pool task: start → Sandbox → renew/result/cleanup → release
  → IO: replenish available tickets
```

连接线程只操作 Pika 和 ticket 状态；HTTP Claim、材料准备、fsync、业务执行均不在 IO 线程。pool 线程通过 `ioloop.add_callback_threadsafe()` 请求 ACK/释放通知；所有回调重验 epoch，旧 tag 不能应用到新 channel。ACK 没有 AckOk：receipt 仅表示在原连接 IO 线程调用发送，不声称 Broker 已确认收到。若连接已失效则不启动 Sandbox，由原 journal / Attempt Lease 收敛；ACK 在网络中丢失仍由 Claim 幂等与 fencing 吸收。

始终保持 `receiving + cancelling + working <= S`，线程池 running+queued 工作数不超过 S，消息正文受既有 dispatch 最大字节校验；没有额外 pending delivery deque。满容量时 consumer 数可为 0，IO loop 持续运行心跳；不存在因容量而关闭连接、延迟 ACK 到业务完成或反复 nack 的路径。业务结束只释放 ticket，IO callback 补一个 consumer；同时多槽释放使用状态重检，不忙等。

注册/QoS/CancelOk 都有真实 IO-loop deadline；把现有 `rabbitmq_claim_handshake_timeout_seconds` 落实为 delivery→cancel→claim/journal/ACK 总预算（默认 30 秒），保持 `< rabbitmq_consumer_timeout_ms / 1000`。Claim HTTP timeout、journal 完成和 ACK receipt 都必须位于剩余预算；超期任务的迟到结果不可再启动或 ACK 新连接。慢 fsync 线程不能被假装取消并立即复用 ticket，须等原任务退出；超时仍通过真实故障路径关闭对应 epoch、保留责任。

连接错误、Broker 主动 `Basic.Cancel`、Control unavailable/auth、取消/注册超时、超协议消息才进入 fault；记录原因并在原 capped reconnect 策略下恢复。连接清理也有 deadline：集中封装固定 Pika 版本的 transport abort（复用现有 Outbox timer/abort 的已采用方法并为 SelectConnection 适配），IO timeout 后终止 stream，不只调用可能等 CloseOk 的 graceful close。启动时探测必需 timer/abort 能力，缺失 fail closed。不得从任意业务线程调用 Pika 私有 transport。

channel 建立时注册 `add_on_cancel_callback`，显式处理服务端主动 `Basic.Cancel`，不能把它当客户端取消的 `CancelOk`。以事件原 `connection_epoch/consumer_tag` 定位 ticket；当前有效 epoch 的主动取消记 `broker_consumer_cancelled` 并进入已有有界 abort/reconnect，不等待心跳断开。receiving/cancelling ticket 不提交 Claim，待原 epoch 终止后按 release-once 释放；已经转交工作线程的 working ticket 仍由该线程退出时释放，保留已 Claim/journal 的责任，不能抢先释放后复用。进入 fault 后禁止原 epoch 新的 Claim/start；若 CancelOk 先到且工作已提交，主动取消不能再次提交工作，未跨 ACK/start 边界的工作按原故障规则停止，已经运行的 Attempt 保留原 Lease/fencing/cleanup 收敛。迟到 CancelOk、重复 Basic.Cancel、channel-close 和任务完成都重检 epoch/tag/phase，同一 ticket 最多提交一次工作、释放一次；已失效 epoch 的事件不得关闭或操作新连接。即使所有 idle consumer 已收到 ConsumeOk、没有 delivery 且 heartbeat 健康，主动取消也必须退出旧 epoch，队列恢复后按真实空槽重建接收。协议依据：[RabbitMQ Consumer Cancel Notification](https://www.rabbitmq.com/docs/consumer-cancel)、[Pika 1.3.2 cancel callback](https://pika.readthedocs.io/en/1.3.2/modules/channel.html#pika.channel.Channel.add_on_cancel_callback)。

断线时：尚未交付的 receiving ticket 释放；cancelling 消息未 claim，交还 Broker 并释放；working ticket 由持有它的工作任务最终释放，已 Claim/journal 的保留原 lease/cleanup；重连仅为剩余空槽注册。关闭/异常回调必须与任务完成使用同一 release-once 状态，防止双释放。用户停止不再补 consumer；已 Claim 的运行按现有 ownership-lost/cleanup 收敛。意外同 tag 第二条投递是协议不变量失败，记固定码并进入 fault，不能进入无界缓存。

选择依据与备选：

- 原 `prefetch=S` 不覆盖 ACK 后的业务占用。保留它并增加最多 S 的未 ACK deque 虽限制内存，但 S 个合法 24 小时任务会让 deque 消息等待超过原 300000 ms consumer timeout，触发取消 consumer/返还消息；因此不能满足 #130 原验收。RabbitMQ 4.3 在支持 cancel_notify 时未必关闭 channel，超时返还也未必增加 delivery-count；不能把这个反例扩大为每次都会导致 DLQ。
- 普通 Blocking `basic_cancel()` 会自动 reject 该 tag 尚未交给回调的 pending delivery；且没有单独 RPC timeout，socket/stack timeout 只覆盖建连，blocked timeout 只覆盖 Connection.Blocked。不能把它当无副作用暂停或硬超时。
- `basic_get` 先预留槽可以有界，但引入持续空队列轮询；动态 QoS=0 表示无限而非暂停。AMQP 1.0 credit 需要更换协议/客户端。
- 异步一次性 consumer 增加每次执行一组 consume/cancel 往返，换取接收责任与真实槽直接对应；不调整 timeout/delivery-limit。上述安全性为协议推导，必须通过真实 RabbitMQ Gate。

官方核对（2026-09-17，RabbitMQ 页面标注 Version 4.3）：[Consumer Prefetch](https://www.rabbitmq.com/docs/consumer-prefetch)、[Acknowledgements](https://www.rabbitmq.com/docs/confirms)、[Quorum Queues](https://www.rabbitmq.com/docs/quorum-queues)、[Pika 1.3.2 SelectConnection](https://pika.readthedocs.io/en/1.3.2/modules/adapters/select.html)、[Pika 1.3.2 cancellation 源码](https://github.com/pika/pika/blob/1.3.2/pika/adapters/blocking_connection.py)。RabbitMQ 4.3 的正常 `basic.nack(requeue=True)` 与失败 delivery count 不同，原 DEFER delayed-retry 保留；不能把它与关闭连接/basic.reject 混为一类。

### 4. 人工处置以一个事务为责任边界

新增 `services/incident_disposition.py`，入口 `dispose_incident(session, execution_id, incident_id, action, expected_generation, idempotency_key, reason_code, principal)`，不得调用 `replay_execution()` 或 `accept_execution()`。`inspect_incident_disposition()` 只读生成 UI 可用动作和原因；POST 必须重新验证，不能信任页面快照。

新增 `execution_incident_dispositions` 审计表：UUID `id`，`incident_id/execution_id` 外键，UUID `idempotency_key`，`request_hash char(64)`，`actor_kind/user_id`，`action`（recover/terminate），枚举 `reason_code`，`outcome/code`，`from_generation/to_generation`，可空 `from_outbox_id/to_outbox_id`，`execution_status`，`created_at`。唯一 `(incident_id, idempotency_key)`；以 `incident_id, created_at, id` 索引分页。请求 hash 只覆盖规范 action/expected generation/reason，不存输入正文、路径、Token、原始请求或自由文本异常。旧 Incident 不补造操作。

`attempts` 继续表示重复观测。API 新增明确 `observation_count`（兼容保留 attempts）、`disposition_count`、`recovery_dispatch_count`（仅实际新代派发）、最近处置/分页链接；同 key 重试不增加次数，GET 和 DLQ 重复观测不增加人工恢复次数。本轮不规定自动恢复次数，因为不增加自动恢复。

锁顺序：先无锁读不可变 execution→adapter 身份；`Adapter → AdapterAdmission → GlobalAdmission → Execution(populate_existing) → 该 Execution 的 Attempt(id 升序) → Slot 0 → Incident(id 升序) → Outbox(generation,id 升序) → 输入引用/审计`。复用 `lock_execution_in_admission_order()`，使 Claim/cancel/terminal 与新服务共享前缀。发现 Slot 被另一 Execution 的 active Attempt 占用时不改变它；原 queued 的重派可等待既有 DEFER，绝不能清空别人的 Slot。

Outbox Relay 仍仅短锁 Outbox 并在事务外发 Broker，不反向索取前缀锁。现有 DLQ 记录/非 delivery-limit 重派路径收敛为同一前缀后再锁 Incident/Outbox，避免扩展后形成 Execution-first/Admission-first 环。普通 cancel 包装器仍 commit；提取 `cancel_execution_locked()` 无 commit helper，供新服务把取消、资源释放、Incident 结果和审计放在同一事务。不得先提交取消再补写审计。

幂等顺序：鉴权后，在锁定范围内先查同 key；同请求 hash 返回原处置 receipt 与当前 Execution 状态，同 key 不同参数返回 409 `idempotency_key_conflict`。新 key 再查 expected generation；冲突和拒绝的稳定结果也在无业务副作用的短事务中留审计（未授权 401/403/404 不写入可见业务审计）。Incident 行串行化同目标并发请求，不依赖页面禁用。所有业务 mutation 与成功 receipt 同提交；失败 rollback 不出现“已恢复”记录。

### 5. 资格矩阵与 Outbox 判定

恢复/终结资格基于受锁的当前事实，不以 Incident kind、年龄或 Broker 统计代替业务状态。

| 当前事实 | recover | terminate |
| --- | --- | --- |
| queued、rabbitmq、未取消、该 Execution 无 active Attempt、身份/材料有效 | 按下表恢复同一 Execution | 调用既有 queued 取消 helper，canonical code，资源一次释放 |
| claimed/准备中/running 或任何 active Attempt | 409 `incident_execution_active`，不创建第二 Attempt | running 仅写 cancel_requested，202 `cancellation_requested`；queued+active 属不一致，409，不直接终态 |
| cancel_requested | 409 `incident_cancellation_pending` | 幂等复用现有取消收敛，不清除取消标记 |
| retry_wait | 409 `incident_execution_not_queued`，留原 retry dispatcher | 依既有取消规则终结 |
| succeeded/cancelled/expired/dead_letter | 200 `execution_terminal`，不重开；Incident 依据事实 resolved | 同样幂等核实，不改业务终态或历史错误码 |
| Incident generation < 当前 generation | 409 `incident_stale_generation`，不修改当前派发 | 200 `stale_incident_ignored`，只关闭旧 Incident 为 ignored，不取消当前代 |
| Incident future generation、缺失身份、message/Adapter/Worker/resource 不匹配 | 409 `incident_dispatch_identity_invalid`，保持人工审查 | 不能由旧/未知消息绕过当前状态；当前 Execution 的常规取消入口仍可按其权限使用 |
| 原冻结材料缺失/过期无有效 Lease/引用不可确认 | 409 `incident_materials_unavailable`，不替换材料 | 资格合法时仍可取消；active 只协作取消 |
| Execution 不存在/非支持 backend | 404 / 409 `incident_execution_unsupported` | 不新建或转换对象，留审查 |

资格按状态优先收敛终态，再判断过时代次；expected_generation 与当前不一致时返回 409 并要求刷新，不先做其他动作。同一个当前 generation 的 Incident 可有多条观测，操作只针对明确 ID。

| 当前代次 Outbox 事实 | 恢复动作 |
| --- | --- |
| 有效 pending、owner/expiry 均空或合法已过期 | 200 `dispatch_already_pending`，沿用既有 row/message/generation/available_at；不加代、不绕过 Relay backoff，不新增恢复次数 |
| pending 且有效 publish lease 尚未到期 | 409 `incident_dispatch_inflight` + 有界 retry_after；不抢 lease、不加代 |
| 有效 published，message_id 与 Incident 匹配，无终止处置 marker | 检查 Outbox headroom 后 generation + 1，创建新 UUID message / pending Outbox；旧 published 保持不变，Incident resolved / `recovery_dispatched` |
| row 缺失，或 payload/route/message/lease 结构无法与冻结身份一致解释 | 409 `incident_dispatch_identity_unverifiable`；不能把坏记录重播成可信任务 |
| published 仅是 `settle_pending_outbox()` 的取消/过期处置 marker | 409 `incident_dispatch_settled`，重读业务终态；不当成 Broker 确认，也不复活 |

新 generation 只发生于确认需要替换当前已投递 dispatch 的人工操作；材料、version、input_config_revision、target/resource/retry/credential/builtin 快照、Attempt 历史、Admission charge 全部保留，attempt_count 不变。原代重复消息由 Claim 的 stale_generation ACK_NOOP 拒绝；新 Claim 获取更高 fence。若 Relay 正在处理旧 row，row lock 与 lease 判定决定冲突，不清除活跃租约。`require_outbox_capacity()` 保留精确保护线；拒绝时不增加 generation。

恢复成功表示重新承担派发责任，不等于业务成功。响应分别返回 operation receipt、Incident 状态及 Execution 当前状态。终结 active 记录时 Incident 保持 open，并把操作标记 cancellation_requested；终态服务在同一既有前缀锁下完成该 terminate 记录的 outcome/Incident resolved，迟到 result/重复 cancel 不重复释放。历史 Attempt cleanup 的责任仍独立存在，不能以 Incident resolved 冒充已清理。

### 6. 冻结材料验证与资源责任

抽出无 commit、无用户可见 Secret 的 `validate_recovery_materials()`；不直接调用会解密/构建运行 payload 的 `build_task_payload()`，也不调用内部 commit 的下载接口。验证：

- version 存在且属于原 Adapter；language/target Worker 与当前 Outbox frozen identity 一致；Worker protocol、语言、isolation 与 builtin capability 有效。临时 offline 不换 Worker，可保留原队列等待；不能把 capability 未确认当成有效。
- ResourceProfile、RetryPolicy、目标及资源快照闭合有效；不刷新成当前 Adapter 配置，不增加 max_attempts。
- JSON/webhook/none 使用原 Execution.input 和 input_snapshot/revision，不做新的用户输入接受事务。
- managed_files 的每个 snapshot artifact ID/ordinal/size/hash 与原 ExecutionInputArtifactLease 一一对应；无缺失/额外项，原租约仍有效且 Blob 存在、大小/hash 可验证。仅当前 Binding 已变更或上传保留时间已过不代表已持租约的 Execution 输入失效；允许原下载合同接受的受 Lease 保护状态，不把 READY 一项当唯一条件。真实文件不存在、删除进行中无法证明保护或未知引用一律拒绝。
- builtin package 的冻结 metadata、已上传材料和 size/hash 有效；Credential 只核原 binding snapshot/ref/字段存在和可用于执行，不向 API/审计暴露明文。不用当前绑定替换原引用。

本地物理校验采用有界流式读，先做可失败的只读预检，再在业务事务中重新核对冻结引用与保护 Lease/材料身份；复用现有文件锁/GC协议保证验证至提交不会被合法回收。文件锁与 DB 锁不得互相反序；若现有 GC 协议无法给出此保证，返回 materials_unavailable，不能写“已验证”后放行。网络和整包下载不放在持锁事务中。额外临时文件在本次结束删除。

恢复不释放任何 Admission/Input Lease，也不创建第二份 charge；合法 queued/retry_wait 取消走原 release-once、lease release、settle Outbox。active 取消只有 terminal/lease recovery 持有效 fence 时才释放 Slot/Admission，保持 journal cleanup 回报与旧 fence 的兼容性。

### 7. API、页面和原因分类

沿现有 `/api/executions/{id}/reliable-detail` 增量返回 Incident ID/generation、观测次数、处置摘要、`recover_available/terminate_available` 和稳定拒绝码。操作为 `POST /api/executions/{id}/incidents/{incident_id}/dispositions`，必须有 UUID `Idempotency-Key`；body 是 action、expected_generation、枚举 reason_code（`capacity_repaired/routing_repaired/operator_cancel/verified_terminal`），成功 200 或 accepted 202，冲突 409、材料不可用 409、格式 422，Outbox 容量复用现有 503/backpressure 合同。审计分页 `GET .../dispositions?before_id=&limit=` 限定 1..100。

GET 复用 Execution read 权限；POST 复用 business principal + `require_execution_access(..., "edit")`，不能仅鉴权 Incident ID 或信任客户端 actor。跨 Adapter 不可见按既有 404 合同，read-only 403；服务事务内再校验访问事实。UI 从后端能力与当前可编辑权限共同派生动作，在 `ExecutionHistoryPanel` 原 Incident 区域显示“恢复此执行 / 终结此执行”、动作原因、观测/处置次数及结果；active 文案明确是请求取消。重复点击重用该次 key，状态刷新后新的明确操作才使用新 key。提交遇到409刷新详情，保留当前选中原 Execution，不跳转到新记录。中英文、键盘和真实 Chrome 双用户权限验收必须完成；Ant Design 精确版本文档须在写 UI 代码前按项目 skill 查询。

按 RabbitMQ [Dead Letter Exchanges](https://www.rabbitmq.com/docs/dlx) 的结构化 `x-death[].reason` 区分 `delivery_limit/rejected/expired/maxlen`。只接受与目标 dispatch queue 对应的已解析事件；不可把旧历史链上的任意 reason、非空数组、`x-delivery-count` 或存在 `delivery-limit` 配置头当当前 delivery-limit 证据。缺失/畸形/未知 reason 记 `unknown`，payload 无效仍走既有 invalid 分类。保留原非 delivery-limit `dispatch_infrastructure_error` 自动重派合同并补原因回归；新 UI不新增后台 recovery。旧数据库没有原 headers 的 Incident 保留原 kind 并注明历史分类不可复核，不批量凭猜测改写。

### 8. 狭义 carry-forward 升级保全

现控制器无绕过 stuck queued 的合法入口，需要作为 #152B 配套修改并独立 review。默认自动 `deploy` 空闲条件不变；新增私有、显式候选绑定的 `carry_forward` 清单/模式，仅面向本机制可向前兼容迁移。清单绑定旧 SHA、新 SHA、schema、候选 Execution/Incident IDs 与快照指纹，不能用通配符或永久设置“忽略 busy”。公开仓库仅放生成/验证器、合成 fixture 和合同。

原 manifest v2 的同 schema 后继只显式允许 `0040_issue152_dispositions → 0040_issue152_dispositions`，其 schema 新增集合为空，不得泛化为任意相同 revision。每个后继候选必须从当前事实生成新的 SHA/controller 绑定 manifest；未知同 revision 或未知向前路径在规划写出 manifest 前拒绝。v2 路径仅接受下述现有责任分类，并要求 `execution_incident_dispositions` 表存在且为空；已有 disposition 在停止任何服务前拒绝规划。既有 `runtime_reconciliation_cursors` 表保留在原 inventory 中，后继不重新执行 0039 seed；当前游标不要求等于初始 `0/0`，应用运行期间正常 reconciler 仍可推进游标。

资格不是只放宽 SQL：queued 必须有关联 open infrastructure Incident、Admission 未释放、无该 Execution 的 active Attempt；禁止其他非清单 queued/retry_wait/running、任意 active Attempt/Slot。先做只读预检，停旧 Control（等待请求退出），复查无 active，再停 Worker/Web；全部停止后再次验证数据库、Slot、kernel cgroup/process、runtime/workspace 和私有 journal。出现 claim/新责任/未知残留立即 attention 或安全恢复旧服务后等待，不继续迁移。

cleanup 分类独立于是否有 Incident：

- `attempt_count=0`、真实 NOT EXISTS Attempt、worker_id/started_at 空、无工作区/journal/Sandbox 证据：原 pending 是 never-claimed 占位，派生 `not_applicable`，不写 completed，也不修改 Execution 原字段；适用于 queued 和已合法取消的终态。
- 有历史 terminal Attempt 且 cleanup=deferred：须确认无活动 Slot/进程，所有待清理责任有可验证原 journal/凭据与受控路径，并确认原 runtime/journal 卷保留；允许作为单独 carry-forward cleanup 责任由新 Worker 现有恢复程序完成真实 receipt。receipt 未到不宣称 cleanup 成功。
- pending/deferred 不能归入以上状态、journal 丢失/不可信、kernel状态未知、还有进程或 active Attempt：阻塞，不能手改字段或删目录通关。

`assets.py` 现有资产指纹不含运行责任。新增独立 manifest：Execution 旧列投影（含 ID/status/generation/input与各冻结快照摘要/cleanup）、Attempt、Slot、Incident、Outbox、Admission、Input Lease/Hold，以及受控 journal/runtime 文件指纹；不把私有内容放进公共回执。停写后、备份后、迁移后且新服务启动前逐项比对；仅允许新增本变更表/默认数据。保留 Broker durable queue/消息、所有 DB/材料/runtime/journal 卷、Token/Master Key。

仍要求候选继承部署历史、精确 HEAD CI成功、迁移链一致、备份可列出、镜像固定、attention 持久、真实执行探针与 Sandbox 检查；控制器改动不能抢在审查前安装。首次带未处置 Incident 升级后，原记录从 UI/API 人工恢复/终结，逐条核原 ID、generation、Attempt/输出、Admission/Lease和cleanup；不得通过新建任务替代旧记录。对照样本采用合成等价状态，私有现场 ID/地址/文件不写公开设计。

### 9. 已完成处置后的受限 Web 同 schema 保全

原记录处置完成后，审计表非空，不能复用第 8 节 v2 的 audit-empty 路径。为交付本组详情输出滚动修复，单独增加 `audited-web-same-schema-v1`，不改变普通空闲部署或 v2 的任何资格。入口是既有 `plan-carry-forward` 的显式 `--mode audited-web-same-schema-v1`；省略 mode 仍生成/验证 v2。未知 mode、v3 缺 mode、把 v3 键混入 v2、错误 schema、额外或缺失键一律拒绝，不按数据库存在审计自动选择模式。

#### 9.1 闭合 Git 差异与对象绑定

v3 仅允许旧/新 revision 都是 `0040_issue152_dispositions`，旧/新完整 migration graph 的路径和文件内容相同，无新迁移。旧部署 SHA 必须是候选祖先，仍要求当前所选 PR 的 eligible 精确 HEAD、已 stage 镜像和原 CI/历史检查。本模式从旧部署 commit 到候选 commit 的**全部** Git 差异只允许下表所列文件；路径按仓库相对路径逐项匹配，不使用目录前缀、glob 或由 manifest 指定的允许列表。

| 分类 | 唯一允许路径 |
| --- | --- |
| 产品源 | `web/src/index.css` |
| UI 回归 | `web/tests/e2e/issue152-popconfirm.spec.ts` |
| 控制器 | `tools/local-preview/carry_forward.py`、`tools/local-preview/preview.py`、`tools/local-preview/deploy.sh` |
| 控制器回归 | `tools/local-preview/tests/test_carry_forward.py`、`tools/local-preview/tests/test_preview.py`、`tools/local-preview/tests/test_preview_locks.py` |
| 操作合同 | `docs/zh-CN/local-preview.md`、`docs/en/local-preview.md` |
| 本组规划 | `openspec/changes/issue161-runtime-reliability/proposal.md`、`openspec/changes/issue161-runtime-reliability/design.md`、`openspec/changes/issue161-runtime-reliability/tasks.md`、`openspec/changes/issue161-runtime-reliability/specs/incident-preserving-upgrade/spec.md` |

产品源差异集合必须恰为 `web/src/index.css`，内容只交付已独立审查的详情输出滚动修复；路径合格不代表任意 CSS 内容已获批准。Backend、Worker、migration、依赖 manifest/lock、Docker/Compose、CI、其他产品源、安装器和未知路径都不允许变化。所有允许项也只能为原普通 `100644` 文件的 `M` 修改；新增/删除/重命名/复制/类型或 mode 变化、symlink、submodule 均拒绝。测试复用上述既有文件，不新增例外路径。

控制器在可信源码缓存对两个完整 commit SHA 解析实际 commit/tree/blob 对象，用 `git diff-tree --raw -r -z --no-renames` 等价的完整树差异生成条目，不能只检查 `web/` 或相信清单的“无 backend 修改”布尔。条目按 path 唯一排序，精确字段为 `status, old_mode, new_mode, old_oid, new_oid, path`；其中 status 只能 `M`、mode 只能 `100644`、OID 必须是实际 blob。`source_diff` 精确为 `{tree_digest, entries}`，`tree_digest` 是既有 canonical digest 对 `{from_tree, to_tree, entries}` 的摘要；tree 值分别为实际 `from_sha^{tree}` / `to_sha^{tree}`，不是清单自行宣称的路径集合。

plan 写出 manifest 前、select 安装 manifest 时、switch 在既有 operation/config 锁内且停止任何服务前，都从真实 Git 对象重算整个差异、tree digest 与旧/新 migration graph，重检所有候选绑定。控制器文件摘要继续独立绑定；候选/控制器变化必须 fresh plan，不重绑已有 manifest。VM release 不需要增加 `.git`；host 重算与现有 VM 的 current SHA、schema、manifest、镜像/容器、控制器、卷身份复核共同形成切换边界。所有候选镜像仍正式构建与核验，不声称源文件不变就等于 backend/Worker 镜像二进制不变。

#### 9.2 v3 清单与选择形状

v3 顶层精确保留 v2 的 `manifest_id, created_at, repo, pr, from_sha, to_sha, from_schema, to_schema, controller_files_digest, migration_graph_digest, old_image_ids, candidate_image_ids, selection, responsibilities, old_runtime_projection, schema_inventory, storage_identity, old_containers, file_evidence, kernel_evidence, manifest_digest`，使用整数 `format_version=3`，仅新增 `mode` 和 `source_diff`。mode 必须精确为上面的固定字符串，所有原私有文件 owner/mode/大小、原子写、摘要、锁和闭合形状校验保留。v2 顶层和原选择形状不加可选键。

v3 的 selection 精确为 `{queued, cleanup_execution_ids, terminal_executions}`；前两项保持原字段和规则。`terminal_executions` 是非空列表，每项精确包含：

| 字段 | 类型和意义 |
| --- | --- |
| `execution_id`、`incident_id` | 正整数，拒绝 bool/float |
| `disposition_id` | 规范小写带连字符 UUID 字符串 |
| `expected_status` | `succeeded`、`dead_letter`、`cancelled` 之一 |
| `expected_generation` | 正整数，拒绝 bool/float |
| `expected_output_digest` | 64 位小写十六进制，按既有 controller `digest(Execution.output)` 定义；null 输出也必须计算摘要，摘要字段本身不可为 null |
| `expected_error_code`、`expected_last_error_code` | null 或匹配 `^[a-z][a-z0-9_]{0,127}$` 的非空稳定码，与实际值和类型精确一致 |
| `expected_attempt_count` | 非负整数，拒绝 bool/float；等于实际 Attempt 数 |

独立业务 oracle 先验证原终态，再由私有选择冻结这些明确预期；校验器不能读到当前值就反向填入“预期”从而无条件通过。现场的原 ID、条数、输出/源码摘要、错误码组合、地址和卷名不写成公共常量，公开测试以不同数量/ID 的合成集合验证同一合同。terminal 的 Execution、Incident 和 disposition ID 各自不得重复，一项只关联一条明确处置。集合关系为 queued 与 terminal/cleanup 分别不相交，terminal 与 cleanup 可以相交。terminal 选择本身不是 cleanup 豁免：凡实际 pending/deferred 清理责任仍须进入独立 fresh cleanup 选择，漏选即拒绝。

`responsibilities.executions` 继续使用原 queued＋cleanup 分类及文件核验；终态合同独立以 selection 对真实关系进行核对，不把 completed terminal 混为新的待清理责任。完整 `old_runtime_projection` 在 v3 仅由原十三张责任表加 `execution_incident_dispositions` 共十四张组成，原 v2 保持十三张。原 schema 全表 inventory 保留；本次同 schema 检查实际列列表和实际 PK 与 baseline 完全一致，不用“baseline 列仍是当前列子集”放过新列。既有 cursor 表保留、不重新 seed 或强制为 `0/0`，运行中正常扫描推进不是保全失败；本扩展不声称把运行中的 cursor 行冻结到 plan 时刻。

#### 9.3 完整审计与终态关系

在同一 `REPEATABLE READ READ ONLY` 事务读取全部审计，真实列列表必须恰为现有 0040 的十七列：`id, incident_id, execution_id, idempotency_key, request_hash, actor_kind, user_id, action, reason_code, outcome, code, from_generation, to_generation, from_outbox_id, to_outbox_id, execution_status, created_at`，实际 PK 必须为 `id`。记录列顺序、PK、按 PK 规范排序的**每行全部列** hash 与总数；缺列、额外列、换 PK、同数换行、幂等键/请求摘要/时间/Outbox 引用或任意其他列变化都拒绝。不能用 API/observer 的窄投影或 count 代替全表证据。原始值仅在只读校验内存中使用，私有清单仍保存摘要，不把幂等键和请求细节写公开输出。

审计表的 ID 集合必须精确等于 terminal 选择的 disposition ID 集合；非空但未选、额外拒绝 receipt、同一 Incident 的额外审计或任一未关联行均阻塞。每行 id/idempotency_key 是有效 UUID、request_hash 是 64 位 lowercase hex、created_at 非空有效；actor 只能是 superadmin/user_id null 或 account/正整数 user_id，并满足既有外键身份。请求摘要按原处置的闭合 intent `{action, expected_generation: from_generation, reason_code}` 的 canonical/JCS 结果验证，不能另造请求或改审计。与关联 Incident/Execution/Outbox 的语义只允许：

| 已完成动作 | 精确关系 |
| --- | --- |
| recover | reason 为 `capacity_repaired` 或 `routing_repaired`；outcome/code 均 `recovery_dispatched`；receipt 的 `execution_status=queued` 保持当时事实；from ≥ 1，to=from+1=当前 Execution generation；两个非空且不同的 Outbox 引用真实存在、同 Execution、分别对应 from/to generation，均 published；当前 Execution 为 succeeded 或 dead_letter |
| terminate | reason 为 `operator_cancel` 或 `verified_terminal`；outcome=`execution_terminal`、code=`execution_cancelled`、receipt status=`cancelled`；from=to=当前 generation；from/to 指向同一已 published 原 Outbox；当前 Execution 必须 canonical cancelled、零 Attempt、null 输出 |

所有关联 Incident 必须属于原 Execution，status=resolved、resolved_at 非空、generation=from_generation、message 与 from Outbox 相符。已 published 的原 Outbox 不要求被覆写为 cancellation marker，其十余列完整原投影继续严格保全。`cancellation_requested`、`dispatch_already_pending`、拒绝/冲突 receipt、未知结果、空或矛盾引用以及仍在 queued/running/retry_wait 的所选 terminal 都不进入本模式。

当前终态逐项匹配独立选择中的 status/generation/output/error/Attempt 预期、原 ID 和全行指纹；succeeded 的两个 error 为 null，至少一次 Attempt且最新成功；dead_letter 的两个 error 为同一个明确的非空预期码，至少一次 Attempt且最新失败码一致；cancelled 的两个 error 均 `execution_cancelled`，无 Claim/start/worker 事实。全部历史 Attempt 保留，编号/实际条数与原终态一致，无 active Attempt/Slot、无保留 Input Lease/Hold、无 replay copy，Admission 已按原状态机释放且 ended_at 有效。queued 的 open Incident/Outbox/冻结材料、Admission 未释放资格仍逐项满足原规则。Adapter/global 的 count/bytes 按全库仍未释放的原责任求和验证，不因一个 Adapter 存在已完成 terminal 而把其其他 queued charge 归零。

fresh cleanup 仍按第 8 节实际证据分类，包含已取消但零 Attempt 的 pending 占位；它只能在真实 Attempt/worker/start/workspace/journal/kernel 全为空时派生 `not_applicable`，原 pending 保持，不补 completed。历史 deferred 仍需完整可信 journal、私有凭据绑定和原卷，未知文件/进程/namespace/FD 或漏选保持阻塞。

#### 9.4 全过程验证与交付边界

新模式在 plan 阶段完成源差异、关系、终态、cleanup 和全表资格检查，任一失败不写 manifest、不停止服务。切换仍先停 Control 并等待请求退出，重读全部关系/十四表与文件责任；通过后才停 Worker/Web/account-web。停写后、pg_dump 与独立 pg_restore list 后、同 head schema 核验后且候选服务启动前，逐次比较完整旧投影、审计、表 inventory/列/PK、资产、材料/journal/runtime、kernel/namespace/FD 和卷/容器所有权；发生新审计、Claim、字段变化或未知读错都保持应用停止和 attention，不自动 restore/downgrade/restart 或 reset cursor。

正式安装继续遵循原 installer、watcher singleton、operation/config 锁序与已审 controller digest；不手换 Web 或给 bypass 环境变量。原精确镜像、Sandbox、真实 Broker→Worker 及 cleanup probe 仍执行。新服务启动后的 probe 只允许自身可明确关联的新增 Execution/Attempt/Outbox 等增量；原所选终态、queued、审计全表和其他保留责任仍需单独核对，不能用 probe 成功覆盖它们的漂移。Ready 后重新封存原终态/queued/审计证据，再由真实 Chrome **只读**检查这些原记录的完整输出/日志，不再次处置。已有独立 offline oracle 的 PASS 保持 offline/PENDING_UI 边界。

验证保留原全部 controller 测试，新增真实 PostgreSQL 十七列完整审计正例，以及同数换行、逐列/隐藏列变化、类型等值漂移、新/漏审计、错误关系、inflight、terminal 结果漂移、漏选零 Attempt pending cleanup、共享 Adapter 的剩余 charge、计划到停写间变化的负例。用独立临时 Git 仓库测试闭合 source diff 的正反例。隔离真实 PostgreSQL 只提供合成 baseline/重复采集、pg_dump/pg_restore list、同 head no-op Alembic、十四表/实际 PK/inventory 保全及并发漂移拒绝证据；不模拟 Control 停服、伪造 kernel/卷身份或另建完整验收环境来计入正式门禁。实际 stop 顺序、kernel/namespace/FD、原卷/镜像和真实 Broker/cleanup probe 由后续固定环境正式更新单独提供，不把纯字典桩或隔离 PG 当作该证据。UI 回归须以原生 wheel 抵达长输出尾部的可见几何为准，覆盖普通/长/截断 JSON、空输出、日志与多 Attempt/Incident、桌面/窄屏、已滚动 Drawer 内 Popconfirm 的键盘/取消/确认和权限，不改变既有 epoch/幂等策略。本扩展止于第一组，不形成后续组通用带审计迁移能力。

## Risks / Trade-offs

- 一次性 consumer 增加往返与状态转换 → 固定 Pika 版本、最小 ticket 状态、真实 Broker fault injection；若协议组合推导未被真实证据证实，不进入最终 Gate。
- cursor 在保留后崩溃会延后一轮 → 持久轮界与显式有界公平验收；不修改业务 lease 补偿。
- 人工恢复实际运行仍可能有业务副作用 → 保留同一 Execution、原重试策略和外部业务幂等键，不承诺全局 exactly-once。
- GC/物理材料核验与 DB 锁配合错误 → 复用原 Lease/文件锁协议，真实删除竞态测试，无法确认即拒绝。
- `published` 不总是 Broker确认，cleanup `pending` 不总是有真实目录 → 处置/部署按完整证据分类，不能按单字段捷径判断。
- 带审计模式容易被误当通用升级或把历史 receipt 状态改成当前终态 → 独立 v3/闭合 Git 路径/全十七列关系校验，v2 audit-empty 保持；只读预期与当前终态分别验证。
- 升级包含旧责任和凭据 journal → 私有备份、候选绑定manifest、升级控制器独立 review；任何未知事实保留 attention，不自动回滚数据库。

## Migration Plan

1. #135 A / B 各自实现、针对性检查、独立提交；无迁移。#134 新增游标/索引迁移，真实旧库升级后验证。#152 A 独立 Worker 提交和 Broker Gate。#152 B 再新增处置审计迁移、服务/UI与部署合同（可多提交但不得提前穿插到 A）。每个检查点保留提交 SHA 与对应证据。
2. 全新库执行完整 Alembic upgrade；从本次基线 schema 带旧 queued/Incident/cleanup样本升级；迁移只新增对象，不清理旧数据、不重建Worker已有约束。ORM schema diff 限定核查新增对象和 #135，不顺手重构整库。
3. 最终 PR head 运行 Backend/Web全量检查、Compose smoke、真实 Broker/DB/Chrome/Gate 与独立 Review。控制器修改单独审查后安装；精确CI HEAD才允许固定预览升级。核对部署镜像SHA、合并SHA及目标环境探针，保留证据。
4. migration/切换失败保留原卷、备份和attention；只有确认旧 schema/旧镜像仍兼容的切换前失败才恢复旧应用。迁移后不自动 downgrade、不清空数据库另建环境；由具体修复继续向前或按明确授权恢复备份。
5. 原处置已完成后的本组 Web 滚动修复按第 9 节生成 fresh v3：先更新并严格验证规划，独立实现/Review 控制器和双语合同，最终 HEAD CI 通过后官方安装、重新 plan/select/status，同 schema 保全并只读复查原终态。原 v2 清单、历史失败证据和业务记录不得覆盖，已有审计不回迁到空表。
6. 尚未运行的测试和用户手工验收保持未完成；依用户收窄后的范围，第一组技术与发布门禁通过后停止，不进入后续组。#135/#134/#152 在用户最终验收前保持开放，PR文字不得自动关闭这些 Issue。
