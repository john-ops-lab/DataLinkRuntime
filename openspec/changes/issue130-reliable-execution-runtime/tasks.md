## 1. 实施前门禁与证据基线

- [x] 1.1 在开始产品代码前记录 `origin/main`、当前分支、HEAD、工作区状态、活动 worktree、open PR 与 Issue #130/OpenSpec 路径；验证：证据明确保留既有 `.agents/ao/*-rules.md` 改动且没有来源不明变化
- [x] 1.2 读取适用 `AGENTS.md`、AO orchestrator/worker rules 与本 change 全部 artifacts，形成 B1→B2→B3 串行执行回执；验证：回执明确一个分支、一个最终 PR、LOCAL_FAST 无官方 Review
- [x] 1.3 对 Pika、RFC 8785 实现与 RabbitMQ 4.3.5 做 Python 3.13/API/许可证最小兼容实验；验证：锁定版本能完成 Confirm、mandatory return、consumer、JCS 官方向量，否则报告 `BLOCKED_DEPENDENCY_GATE` 且不写依赖产品代码
- [x] 1.4 解析 RabbitMQ 4.3.5 精确镜像 digest 与必需 feature flags；验证：发布证据使用 immutable digest、无 `latest`，并记录单节点 Quorum 非 HA 边界

## 2. Batch 1 — Additive Schema 与配置

- [x] 2.1 扩展 Control/Worker 配置为 RabbitMQ、Admission、Outbox、Attempt Lease、Idempotency/Hold 与 Resource Profile 的有界字段；验证：默认值、非法边界和交叉不变量单元测试通过，错误输出不含 RabbitMQ URL userinfo
- [x] 2.2 新增 additive Alembic migration，扩展 Execution 状态并加入 `dispatch_backend/generation/queue/retry/resource/admission/replay` 字段；验证：current-main upgrade 后全部历史行确定 backfill 为 legacy，重复 inventory 计数一致
- [x] 2.3 新增 Idempotency、Adapter/Global Admission 与 Outbox schema/constraints/index；验证：唯一 key/generation、非负 counter、pending lease查询与 FK/retention 约束数据库测试通过
- [x] 2.4 新增 ExecutionAttempt 与 Adapter Slot 0 schema/constraints/index，但保持未启用；验证：一个 Execution 最多一个 active Attempt、一个 Adapter Slot 最多一个 active Attempt 的并发数据库测试通过
- [x] 2.5 新增 Schedule policy/outcome、Infrastructure Incident 与 Artifact Hold schema；验证：旧 Schedule 幂等回填 `coalesce_latest/100/86400`，聚合 outcome/Hold 约束测试通过
- [x] 2.6 扩展 Worker protocol DB CheckConstraint、请求 schema 与配置允许 v3，同时保持当前 minimum 为 1/2；验证：v1/v2 原注册测试通过、v3 可保存、minimum 未被设置为 3
- [x] 2.7 在 fresh PostgreSQL 与 current-main schema snapshot 分别执行 `alembic upgrade head`；验证：两条路径均成功且 downgrade/旧二进制边界在迁移文档中明确 fail closed

## 3. Batch 1 — RabbitMQ Topology 与 Transactional Outbox

- [x] 3.1 增加 RabbitMQ 4.3.5 Compose service、持久卷、非 guest Credential、healthcheck 与仅本地可选 management profile；验证：`docker compose config` 不泄露真实凭据，Broker 重启后 durable Queue/Message 保留
- [x] 3.2 实现幂等 topology bootstrap 与漂移检查，声明 direct exchange、每 Worker Quorum Queue、Infrastructure DLX/DLQ、reject-publish/length/bytes/delivery-limit/at-least-once策略并保留 Relay/运维 headroom；验证：重复 bootstrap 无变化，不兼容既有 Queue使health fail而不删除消息，且 max-length/max-length-bytes 被记录为允许 bounded in-flight overshoot 的近似最终拒绝保护而非精确业务硬上限
- [x] 3.3 实现最小 dispatch message schema/serializer 与安全扫描；验证：persistent message 只含允许字段，Code/Input/Secret/Token/storage key/path 均不存在
- [x] 3.4 实现 Outbox 创建、due lease领取、bounded backoff 与 owner条件更新；验证：`FOR UPDATE SKIP LOCKED` 并发测试无重复 lease，数据库锁不跨网络 publish，任何失败重试都不删除旧 pending 消息
- [x] 3.5 实现 Pika Relay 的 mandatory Publisher Confirm 与 timeout/return/nack处理，以及有限 publisher channel 数、并发 publish 数和 confirm 在途窗口；验证：正常 publish 标记 published，unroutable/nack/timeout/connection loss保持 pending并按 capped backoff 重试，无 unbounded publish buffer
- [x] 3.6 注入 Confirm ack 后 DB mark 前崩溃；验证：lease到期会重复publish但同 `(execution,generation)` 事实不丢失，Relay重启可继续
- [x] 3.7 实现 Outbox pending count/bytes/oldest精确保护与 Relay/Broker health/metrics/alerts；验证：任一 DB 阈值触发 `outbox_backlog_full`，`messages_ready` 不参与业务判断，既有 pending仍恢复且普通 retention不删除，queue reject/overshoot/headroom 可观测

## 4. Batch 1 — 可靠 Ingress、Admission、Idempotency 与 Schedule

- [x] 4.1 提取统一 `accept_execution` 事务路径并复用 #127 InputConfig/Lease/snapshot；验证：Manual、run-now、Schedule、Webhook 的成功事务都包含 Execution+Admission+Outbox，任一故障全回滚
- [x] 4.2 实现 Adapter/Global count+logical-bytes Admission 与 `admission_released_at` 条件释放；验证：并发竞争最后名额最多一个成功，重复终态/cancel/reconciler不负数或双释放
- [x] 4.3 实现 Admission Reconciler 与审计指标；验证：人工构造 counter漂移后可幂等修正且不修改Execution业务状态
- [x] 4.4 实现 Idempotency-Key校验、raw-key SHA-256、JCS payload hash、同Key返回与conflict；验证：RFC向量、键顺序/Unicode/数字、同Key同/异payload、配置在重试间变化测试通过且日志无原Key
- [x] 4.5 实现 Idempotency 24小时/非终态延长与 Execution retention协调；验证：有效记录阻止关联Execution先删，终态过期后cleanup可重跑
- [x] 4.6 调整固定 Worker门禁为“存在且capability兼容即可排队”，保留缺失/删除/不兼容409且不reroute；验证：offline返回202 queued，invalid target无Execution/Lease/Outbox副作用
- [x] 4.7 实现 legacy/rabbitmq backend 的服务层状态转换与 retention筛选；验证：历史 failed/timeout可读，新RabbitMQ失败只进入retry_wait/dead_letter，所有非终态均不被清理
- [x] 4.8 实现 Schedule policy字段、旧请求省略保持、三种策略与数据库时间cursor；验证：coalesce/queue-every/skip、Admission满、DST与多Scheduler并发测试通过
- [x] 4.9 实现有界 Schedule outcome聚合与重建验证器；验证：大量missed points记录有界且按冻结Cron/timezone重建count/first/last完全一致
- [x] 4.10 更新 Manual/Webhook/Run-now API返回、429/503/409稳定code与 `Retry-After`；验证：OpenAPI/schema与入口集成测试覆盖事务提交前失败、提交后响应丢失和Broker outage

## 5. Batch 1 — Web、国际化与运行状态

- [x] 5.1 在修改 Ant Design UI前读取 `.agents/skills/antd/SKILL.md` 并查询5.29.3精确API/demo/token/semantic snapshot；验证：实施记录包含实际CLI输出且manifest版本仍为React19/antd5.29.3/ProComponents2.8.10
- [x] 5.2 扩展Web状态类型、颜色/标签、Header/list/detail/watcher以兼容legacy与queued/retry_wait/dead_letter/expired；验证：类型检查与Vitest覆盖offline queued、retry countdown、legacy timeout
- [x] 5.3 实现Schedule三策略与catch-up表单、服务端权威保存和outcome展示；验证：旧字段省略不重置、409/422保留草稿、zh-CN/en字段/插值一致
- [x] 5.4 实现429/503/worker-invalid的队列反馈，不把offline/busy误报失败；验证：组件测试覆盖Retry-After、等待Worker与服务端状态刷新
- [x] 5.5 添加Queue/Attempt/Incident/Replay占位详情的轻量加载边界；验证：列表不携带大input/output/log，detail不暴露routing key/Token/storage key/path
- [x] 5.6 运行Web `npm run lint && npm run typecheck && npm run test && npm run build`；验证：全部退出0且无新增raw i18n key/版本漂移

## 6. Batch 1 — Candidate Gate

- [x] 6.1 运行Batch1相关Backend Ruff/format/Mypy/pytest、fresh+upgrade migration和PostgreSQL并发测试；验证：全部失败均修复或标记 `BLOCKED_B1_GATE`，未全绿不勾选本项
- [x] 6.2 在隔离Compose运行RabbitMQ启动/重启/outage/overflow/unroutable/Confirm ambiguity与Outbox recovery矩阵；验证：已接受Execution不丢、DB Admission/Outbox保护线准确、Broker bounded overshoot/reject 与 Relay headroom 可观测、无drop-head/孤儿Outbox
- [x] 6.3 证明Batch1禁止项仍成立：minimum≠3、`uq_executions_active_adapter`存在、legacy Claim启用、RabbitMQ ingress默认off；验证：自动断言全部PASS
- [x] 6.4 形成Batch1 checkpoint commit与Candidate SHA，清点差异只属于Issue #130及受保护AO规则；验证：工作区状态、diff、测试证据绑定同一SHA
- [x] 6.5 由当前主代理Sol对Batch1 exact SHA做只读架构/并发/安全审计并修复所有finding；验证：最终审计PASS绑定最新SHA，且不声称AO官方Review、不创建PR

## 7. Batch 2 — Worker v3 Consumer 与 Attempt Claim

- [x] 7.1 扩展Worker注册/heartbeat保存v3 isolation matrix并对v1/v2保持兼容；验证：bool/float/string/unknown协议fail closed，v2看不到RabbitMQ backend
- [x] 7.2 实现v3 Pika Consumer专用线程、`prefetch=execution_slots`、本地Semaphore与服务级pause/backoff；验证：slots满不无界拉取，Control/auth故障不逐条热循环
- [x] 7.3 实现Control v3 Claim API与五态decision schema；验证：backend/generation/target/due/protocol/capability/slot矩阵逐项覆盖稳定reason
- [x] 7.4 实现Claim事务创建Attempt、递增attempt_no/fencing、绑定Slot0并`queued→running`；验证：同Execution/同Adapter并发Claim最多一个EXECUTE且无部分副作用
- [x] 7.5 实现Attempt-scoped Claim/Cleanup Token hash与constant-time验证；验证：Token不可互换、原文不落DB/URL/Rabbit/日志
- [x] 7.6 扩展Worker私有Attempt journal，在Claim后以0600临时文件+fsync+rename持久后ACK；验证：journal内容最小、ACK不等待terminal、写入失败不启动Adapter
- [x] 7.7 实现Worker start/renew/progress/result/prepare-failed API与fence条件更新；验证：默认60/15秒、旧fence/非owner拒绝、重复同terminal幂等
- [x] 7.8 实现Attempt Lease Reconciler与Slot条件释放；验证：claimed/running过期均收敛worker_lost，Result竞争只有一个权威终态
- [x] 7.9 扩展cancel覆盖queued/retry_wait/active Attempt与旧message ACK_NOOP；验证：cancel/result并发只释放Admission/Slot/Lease一次
- [x] 7.10 复用v2 Managed Files下载、Token、Context与cleanup receipt到v3 payload；验证：三语言文件顺序/校验/清理不回归，Rabbit消息无文件/路径

## 8. Batch 2 — Retry、Dead Letter、Replay 与 DLQ

- [x] 8.1 实现闭合Retry Policy schema/default/snapshot与保守错误分类；验证：默认只重试platform transient/worker_lost，业务/timeout/resource错误默认dead-letter
- [x] 8.2 实现Attempt terminal到succeeded/retry_wait/dead_letter/cancelled的单一事务服务；验证：attempt_count只在Claim增加，Admission/Slot/Hold/cleanup副作用各一次
- [x] 8.3 实现due Retry Dispatcher的generation+1与唯一Outbox；验证：多Dispatcher并发和crash/retry不重复generation/Outbox
- [x] 8.4 实现Business Dead Letter detail与Replay新Execution；验证：旧历史不可变、Replay走新Admission/snapshot/Outbox且非dead-letter transition拒绝
- [x] 8.5 实现Managed File 7天Hold、held count/bytes Gate、到期/purge与GC保护；验证：既有任务dead-letter不因保护线失败，Hold到期后Replay稳定返回 `dead_letter_input_expired`
- [x] 8.6 实现Infrastructure DLQ consumer/reconciler与DB Incident；验证：poison/delivery-limit可关联Execution/generation，不能留下Broker-only孤儿或误写Business Dead Letter
- [x] 8.7 实现Attempt/Retry/DLQ/lease/resource低基数指标与脱敏日志；验证：metrics不使用execution/key/token高基数label，secret/path扫描全空
- [x] 8.8 扩展Web Attempt timeline、retry wait、dead-letter/replay/Incident与capability状态；验证：权限、服务端权威、zh-CN/en与轻量列表测试通过

## 9. Batch 2 — Dark Launch 与迁移演练

- [x] 9.1 实现RabbitMQ ingress canary/测试入口，默认生产gate仍off；验证：只有明确canary创建rabbitmq Execution，普通Manual/Schedule/Webhook继续legacy
- [x] 9.2 实现migration inventory/dry-run，输出schema、legacy counts、protocol distribution、Rabbit/Outbox/Sandbox readiness且脱敏；验证：重复运行只读、计数稳定
- [x] 9.3 实现legacy pending事务迁移工具与already-converted收敛；验证：提交前崩溃保持legacy，提交后响应丢失重跑不重复Outbox并完整保留#127快照/Lease
- [x] 9.4 演练legacy running drain边界；验证：工具拒绝中途转换running，未清零时Cutover Gate明确失败
- [x] 9.5 注入duplicate dispatch、ACK loss、ACK后崩溃、journal失败、Control分区、lease expiry与stale Result；验证：无第二active Attempt、无旧fence覆盖、每个Execution可解释收敛
- [x] 9.6 注入Broker delayed defer与同Adapter消息拥塞；验证：DEFER不增加attempt_count、无即时nack热循环、其他Adapter仍可使用slots
- [x] 9.7 运行三语言none/json/managed-files v3 canary及cleanup recovery；验证：input/output/log/Token边界与legacy v1/v2回归均PASS

## 10. Batch 2 — Candidate Gate

- [x] 10.1 运行Batch2相关Backend全量静态检查/pytest、Rabbit/DB并发与Worker故障矩阵；验证：全部失败修复或 `BLOCKED_B2_GATE`，未全绿不勾选
- [x] 10.2 自动断言Batch2结束仍为dark launch：minimum≠3、旧索引存在、legacy Claim启用、普通新流量legacy、Sandbox尚未冒充通过；验证：全部PASS
- [x] 10.3 形成Batch2 checkpoint commit/Candidate SHA并核对不含不可逆Cutover；验证：差异/测试/故障证据绑定同一SHA
- [x] 10.4 由当前主代理Sol对Batch2 exact SHA做只读Attempt/ACK/Lease/Retry/迁移审计并修复finding；验证：最新SHA审计PASS，仍不创建PR或声称AO官方Review

## 11. Batch 3 — Linux Resource Sandbox

- [x] 11.1 在目标Linux环境先实现并运行disposable cgroup v2/tmpfs最小probe；验证：真实创建/limit/kill/unmount/cleanup全PASS，否则报告 `BLOCKED_SANDBOX_RUNTIME` 并禁止Cutover
- [ ] 11.2 实现/文档化精确delegated cgroup subtree与Compose host provisioning，保持Worker `privileged:false`且不挂Docker socket；验证：配置审计只含批准的最小capability/mount
- [ ] 11.3 实现Sandbox startup preflight与Worker capability registration；验证：静态文件存在不算PASS、任一probe失败使 `rabbitmq_execution_v3=false`
- [ ] 11.4 实现per-Attempt cgroup CPU/Memory/swap/PID配置与进程归属确认；验证：Adapter第一行代码前全部limit已生效且Agent在cgroup外
- [ ] 11.5 实现受控Workspace bounded tmpfs、mount/PID namespace与open-files/no-new-privileges；验证：多文件总量硬限、Adapter看不到cgroup控制面/platform credential
- [ ] 11.6 实现Supervisor bounded IPC、wall timeout、`cgroup.kill`、unmount/cgroup cleanup与residue journal；验证：cancel/timeout/crash后进程树为空，重启扫描只处理有权属marker的残留
- [ ] 11.7 实现Resource Profile快照到v3 payload并校验Worker capability/上限；验证：排队后配置变化不追溯，缺失/越界payload在副作用前fail closed

## 12. Batch 3 — 运行期有界与依赖准备

- [ ] 12.1 把`.log`/spool改为硬字节上限并使用固定内存ring/pending-progress队列；验证：无换行/高频日志洪水下文件、RSS、队列均保持边界
- [ ] 12.2 把Output `read_bytes()`路径替换为stat + `limit+1` bounded stream；验证：超大output不整文件入内存且size/preview/truncated/error准确
- [ ] 12.3 让Python/npm/Maven dependency preparation进入Attempt cgroup/timeout/log/tmpfs边界；验证：慢下载、日志洪水、fork、磁盘满只终止当前Attempt
- [ ] 12.4 实现只读已验证version cache与global byte reservation/low-watermark/bounded atomic promotion；验证：并发cache miss不超卖、失败staging可清理、Adapter不能写共享cache
- [ ] 12.5 实现memory/pids/disk/timeout/sandbox-prepare稳定error与resource usage采集；验证：Control/Web zh-CN/en映射一致且无宿主路径
- [ ] 12.6 验证Worker Agent reserve与多slots合计预算；验证：所有slots在limit压力下Agent仍heartbeat/renew/cancel/report，Control/Rabbit/Postgres健康

## 13. Batch 3 — Sandbox 故障与 Candidate Gate

- [ ] 13.1 对Python/JavaScript/Java分别注入CPU、Memory OOM、fork bomb、tmpfs fill、FD与wall timeout；验证：每次只终止目标Attempt，稳定error/cleanup正确，其他Attempt继续
- [ ] 13.2 注入log flood、超大Output、dependency timeout/cache low-watermark与cleanup residue；验证：运行期资源全有界、startup recovery幂等、日志无Secret/path
- [ ] 13.3 在真实Linux cgroup v2 Compose运行startup preflight和完整fault matrix；验证：不可用环境明确fail closed，不能以macOS/模拟结果替代
- [ ] 13.4 运行Batch3相关Backend/Web/Compose/三语言测试与安全扫描；验证：所有失败修复或 `BLOCKED_B3_GATE`，全绿前不进入Cutover
- [ ] 13.5 形成Sandbox checkpoint Candidate SHA，由当前主代理Sol做exact-SHA只读资源/安全审计并修复finding；验证：最新SHA审计PASS且尚未执行不可逆Cutover

## 14. Batch 3 — Final Cutover

- [ ] 14.1 记录数据库backup/restore实测、schema、legacy pending/running、Worker protocol/isolation、Rabbit/Outbox readiness；验证：任一preflight不明或restore未证明则Cutover fail closed
- [ ] 14.2 drain legacy running并对legacy pending逐条选择drain或幂等migrate；验证：running不转换、pending迁移可重跑且最终legacy active为0
- [ ] 14.3 仅在全部Worker v3+isolation后开启普通Manual/Schedule/Webhook RabbitMQ新流量；验证：新Execution全为rabbitmq，legacy Claim查询永不读取它们
- [ ] 14.4 运行同Adapter高并发Claim/Result/Recovery验证Slot0权威；验证：active Attempt始终≤1、不同Adapter可并行、无counter/slot泄漏
- [ ] 14.5 Slot防线PASS后把minimum protocol设为3并验证v1/v2明确拒绝；验证：没有silent fallback或需要继续服务的旧Worker
- [ ] 14.6 使用单独Cutover migration退役 `uq_executions_active_adapter`，并在precondition不满足时拒绝执行；验证：索引删除前后并发不变量均由Slot数据库测试证明
- [ ] 14.7 legacy pending/running清零后关闭legacy execution Claim入口但保留历史读取/兼容恢复代码；验证：旧端点明确不可领取、新历史API不回归
- [ ] 14.8 运行Post-cutover invariant工具两次；验证：legacy active=0、queued有generation+Outbox/Incident、running恰一Attempt+Slot、无双backend/orphan Outbox/无主DLQ且二次结果稳定
- [ ] 14.9 演练Cutover前关gate与Cutover后兼容Control drain/repair回滚；验证：不使用旧二进制解释新row、不执行破坏性schema downgrade

## 15. 最终文档、全量回归与 OpenSpec Gate

- [ ] 15.1 同步README、zh-CN/en产品/架构/部署/API、`.env.example`与故障runbook，明确RabbitMQ单节点非HA、ACK-on-claim、Sandbox Linux边界和rollback；验证：docs链接/双语成对检查通过且无本机绝对路径/真实凭据
- [ ] 15.2 运行Backend `uv sync --frozen`、Ruff、format check、Mypy与full pytest；验证：全部退出0并记录精确Candidate SHA
- [ ] 15.3 运行Web `npm ci`、lint、typecheck、full Vitest与production build；验证：全部退出0，React/AntD/ProComponents版本未漂移
- [ ] 15.4 从fresh与current-main数据库运行完整Alembic/Cutover路径，并做幂等inventory/reconciler/retention；验证：迁移、约束、回滚边界与数据计数全PASS
- [ ] 15.5 在隔离Linux Compose运行Broker outage/restart/overflow/Confirm ambiguity、Control/Worker crash、三语言Task/Schedule/Webhook/managed-files/replay与post-cutover smoke；验证：已接受任务不丢、所有资源/队列有界
- [ ] 15.6 使用真实浏览器验证zh-CN/en和1280/1440/1680/1920的Queue/Schedule/Dead Letter/Worker capability主路径；验证：无raw key/横向溢出，键盘与disabled原因可达，保留应用供人工验收
- [ ] 15.7 运行密钥/敏感路径扫描与Git差异审计；验证：公开可达文件不含Token、Secret、Rabbit URL userinfo、日志/数据库/本机路径或无关生成物
- [ ] 15.8 更新所有已真实完成任务复选框并运行 `openspec validate --specs` 与 `openspec validate issue130-reliable-execution-runtime --type change --strict --no-interactive`；验证：两条命令全绿，未验证/人工任务保持未勾选

## 16. 唯一 PR、Hosted CI 与 AO 官方 Review

- [ ] 16.1 在所有本地Gate PASS后形成最终Candidate commit并停止修改；验证：工作区干净、diff范围正确、测试/审计/OpenSpec证据绑定同一HEAD/tree
- [ ] 16.2 fetch远端并核对origin/main、open PR与分支无漂移后只push `codex/issue130-reliable-runtime`；验证：远端branch SHA等于最终Candidate且没有第二个Issue130 PR
- [ ] 16.3 创建唯一非Draft `REMOTE_RELEASE` PR并完整填写Issue/OpenSpec、B1/B2/B3证据、迁移/rollback和剩余人工验收；验证：PR base/head、文件树与公开安全检查正确
- [ ] 16.4 等待Hosted CI针对精确PR head全部完成；验证：任何失败均修复或明确BLOCKED，修复产生新SHA后重跑受影响本地Gate与Hosted CI
- [ ] 16.5 触发AO官方Claude Review并确认它审查同一精确PR head；验证：不存在以Sol/LOCAL_FAST冒充外部Reviewer，Review输出可关联head SHA
- [ ] 16.6 修复全部AO Review finding并循环Hosted CI + exact-head AO Review直到PASS；验证：最终CI与Review同时绑定最新SHA，旧SHA证据只作为历史
- [ ] 16.7 将PR状态报告为 `READY_FOR_USER_ACCEPTANCE`，不自动merge/release；验证：实现、本地Gate、PR、Hosted CI、AO Review、merge、release与用户PASS/FAIL分别列明
- [ ] 16.8 由用户在保留应用中执行最终视觉/业务验收并明确PASS/FAIL；验证：只有用户明确PASS后才勾选本项，自动测试或AO Review不得代替
