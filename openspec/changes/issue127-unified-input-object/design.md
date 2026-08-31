## Context

动机见 [proposal.md](proposal.md)。当前基线的三个事实直接约束方案：

1. `POST /api/adapters/{id}/executions` 直接把请求体 `input` 写入 Execution；`adapter_schedules.input` 又持有另一份 JSON，Manual 与 Scheduler 没有共同 resolver。
2. Execution 目前只固定 AdapterVersion、JSON input、Worker 和单次 timeout；Worker claim payload 无协议版本和逐 Execution Token，Workspace 使用系统临时目录并在 `finally` 中 `shutil.rmtree(..., ignore_errors=True)`，无法证明崩溃/挂起后的清理结果。
3. 三语言 Runtime 只提供 `config/secrets/logger`，Compose 只有 PostgreSQL 与 Worker Runtime 持久卷；文件能力必须同时跨越 Control schema/API、物理存储、Worker 协议、Runtime Context、Web 和发布迁移。

现有可复用基础包括：PostgreSQL Adapter 行锁与 active Execution 唯一索引、Schedule `FOR UPDATE SKIP LOCKED`、Worker 主动 claim、Execution 终态幂等、Adapter ACL、双入口认证/CSRF、三语言独立子进程、i18next 双语检查、Alembic 与隔离 Compose smoke。方案不引入 MQ、对象存储 SDK、通用上传框架或第二套 UI 框架。

## Goals / Non-Goals

**Goals:**

- 用一条受锁领域路径将“保存的当前输入”转换成不可变 Execution input/snapshot/Lease。
- 让 Blob 生命周期和数据库元数据在跨介质失败下可补偿、可审计、可重试，不制造伪事务。
- 对上传配额、过期、删除、Adapter 删除、Worker 丢失和 Workspace 清理给出有限状态机与单向收敛路径。
- 在 v1/v2 Worker 与旧 Web 兼容窗口内保持已发布合同可运行，并用 feature flag 防止半成品暴露。
- 保持 `Execution.input` 与 `handle(context, input)` JSON 合同；文件另由三语言 Context 访问。

**Non-Goals:**

- 不实现 remote_files、S3/FTP/NAS、通用文件浏览/分享/历史下载、跨 Adapter 去重或引用计数。
- 不把输入配置放进 AdapterVersion，不让每次 Execution 覆盖 Adapter 输入，不新增 overlap/misfire/自动重跑。
- 不把 `0444/0555` 描述为同 OS 用户下的安全隔离，不支持 LocalFileArtifactStore 多 Control writer。
- 不做破坏性回滚 migration；旧 Schedule input 列的最终删除属于观察窗口后的独立变更。

## Decisions

### 1. 以 AdapterInputConfig 为唯一产品真值

每个 Task Adapter 使用 `adapter_input_configs.adapter_id` 作为 PK/FK；核心字段为：

```text
adapter_id PK/FK adapters.id ON DELETE CASCADE
source_type none|json|managed_files|remote_files
json_value JSONB nullable
retention_mode system_default|custom|manual_delete
retention_seconds nullable
revision BIGINT NOT NULL CHECK revision > 0
created_at / updated_at
```

数据库 check 约束 source/type 专属字段的可表示组合；API schema 做更友好的请求校验。`remote_files` 保留在枚举中以固定未来历史展示类型，但写 API 显式拒绝。GET 响应动态计算 `valid_for_run/invalid_reason`，避免缓存字段在 Artifact 到期或治理后失真。

备选方案是把输入写进 AdapterVersion；否决原因是代码保存不应意外改变运行输入 revision，文件 retention 和 Binding 也不属于代码不可变快照。另一备选是保留 manual/schedule 两列并抽象 UI；它仍有两个真值，无法满足 run-now 与 Scheduler 一致性。

### 2. 单一 resolver 在 Execution 创建事务内返回运行材料

新增领域服务概念 `resolve_adapter_input_for_execution`，但 API/调用者不自行拼装输入。事务顺序：

```text
锁 Adapter
→ 涉及 Scheduler 时锁 Schedule
→ 锁 InputConfig
→ 锁当前 Binding
→ 按 Artifact id 升序锁 Artifact
→ 校验 source/revision/READY/expiry
→ 创建 Execution 与不可变 snapshot
→ 为文件插入 Lease
→ 提交
```

resolver 返回 `runtime_input`（JSON 原值或 null）、公开 snapshot 和仅供 Worker payload builder 使用的内部 artifact material。Manual、Scheduler、schedule run-now 均调用同一创建服务；run-now 固定 `trigger=manual` 且不锁后修改 Schedule cursor。active Execution partial unique index继续作为并发最终防线。

备选方案是先解析再在另一事务创建 Execution；否决原因是配置/Artifact 可在两个事务间被替换或到期，快照与 Lease 不可证明一致。

### 3. Binding 只保存当前集合，历史由 Execution snapshot 承担

`adapter_input_artifact_bindings` 使用 `(adapter_id, artifact_id)` PK、`(adapter_id, ordinal)` unique、`ordinal BETWEEN 0 AND 7`，并通过 `(artifact_id, adapter_id)` 复合 FK 强制同 Adapter 所有权。每次成功保存重建当前行并写入同一 `input_config_revision`，不保留历史 revision Binding。

保留历史 Binding 的备选方案会把配置历史误变成可复用文件库，并延长 Blob 保留；Issue 只要求 Execution 审计，因此 snapshot 是唯一历史事实。

### 4. 使用数据库 reservation 与平台容量账户封闭并发超卖

`managed_input_upload_reservations` 与每个 `UPLOADING` Artifact 一对一；`upload_session_id` 和 Artifact 的 `upload_reservation_id` 单列 unique，复合 FK 强制同 Adapter。状态为 `ACTIVE/CONSUMED/CANCELLED/EXPIRED`，writer 只可续租 ACTIVE session。

平台使用一个 `managed_input_capacity(id=1, actual_bytes, reserved_bytes, updated_at)` 单例计数行；Adapter 级用量在已锁 Adapter 下从有索引 Artifact/reservation 聚合，避免额外 per-adapter counter 与删除漂移。上传事务先锁 Adapter，再锁平台 capacity 行，检查：

```text
adapter_actual + adapter_active_reserved + requested <= adapter_quota
platform.actual_bytes + platform.reserved_bytes + requested <= platform_quota
filesystem_free - requested >= min_free_space
```

成功创建 ACTIVE reservation 时增加 `reserved_bytes`；完成时在同一事务按实际大小转换到 `actual_bytes`；取消/过期只允许条件状态更新并释放一次 reserved charge。实际字节超过 reservation 时，writer 在继续写前重复同一锁序原子扩容，否则停止。

备选方案是每次用 `SUM` 扫描全平台；高并发下无法对不同 Adapter 的提交形成单一串行点。仅信任 Content-Length 也被否决，因为请求可缺失或伪造。

### 5. 数据库动态设置与部署设置分权

`managed_input_settings(id=1)` 是可热修改产品策略，初始常量与验证范围如下；所有字节使用整数且禁止 null/负数/无限值：

| 字段 | 初始值 | 允许范围/不变量 |
|---|---:|---|
| `default_retention_seconds` | 86400 | 3600..2592000 |
| `max_file_bytes` | 104857600 | 1048576..2147483648 |
| `platform_quota_bytes` | 10737418240 | 1048576..10995116277760 |
| `adapter_quota_bytes` | 1073741824 | 1048576..platform quota |
| `allow_manual_delete` | true | 非空 boolean |
| `max_custom_retention_seconds` | 2592000 | default retention..31536000 |
| `min_free_space_bytes` | 1073741824 | 67108864..1099511627776 |
| `staged_ttl_seconds` | 3600 | 300..86400 |

`system_default/custom` 在保存事务中以数据库 `clock_timestamp()` 具体化 `expires_at`；`manual_delete` 为 null。管理设置变更不追溯，只有新绑定或用户明确重存 retention 才重新计算。

物理位置、开关和 loop interval 继续用 Issue 指定的 `DLR_*` 环境变量。claim/recovery/cleanup 默认与范围严格采用 Issue 合同：300（30..86400）、60（10..3600）、5（1..60）、20（5..300），并检查 attempt <= total < grace。选择数据库/环境分权而非全部动态设置，是因为挂载和进程 loop 无法在不滚动部署时安全迁移。

### 6. LocalFileArtifactStore 是小而严格的端口

Control 业务层只依赖 `put_part/commit/open/delete/stat/quarantine` 等窄接口，首个实现为 LocalFileArtifactStore，不新增依赖。布局使用随机 key 分片：

```text
DLR_ARTIFACT_STORE_ROOT/
  objects/<prefix>/<storage-key>
  parts/<prefix>/<storage-key>.part
  quarantine/<prefix>/<storage-key>
```

`parts` 与 `objects` 必须在同一挂载；commit 使用原子 rename。打开/删除前校验 key 格式、根目录归属、非 symlink，原始文件名永不拼路径。对象已经不存在时 delete 幂等成功。低频 audit 只隔离超过安全宽限且数据库无 Artifact/deletion job 记录的合法随机对象，不按用户文件名或宽泛 glob 删除。

直接把文件放 PostgreSQL 的备选方案会放大 WAL/备份并破坏 Worker 流式下载；跨文件系统临时目录会失去原子 rename，均否决。

### 7. 上传与绑定明确跨介质补偿边界

上传流程：数据库创建 reservation + UPLOADING 元数据 → 流式写 `.part` 并计算大小/SHA-256/续租 → 原子 rename → 数据库条件事务 `UPLOADING→STAGED` 与 `ACTIVE→CONSUMED`。最后事务失败时补偿删除最终对象；补偿失败由 audit/TTL 以随机 key 收敛。

绑定流程不碰文件系统：校验 STAGED/READY → 原子替换 Binding/revision/retention → 新 STAGED 变 READY → 被移除 READY 变 `PENDING_DELETE`。保存失败时 STAGED 留待用户重试或 TTL。

独立 multipart Web client 是必要的，因为现有 JSON request helper 无上传进度且不能安全构造 FormData；它复用同一认证/CSRF/401 处理，不引入第三方上传库。

### 8. 到期、GC 和 Adapter 删除使用两个删除载体

仍有 Adapter 元数据的 Blob 由 Artifact 自身状态治理：

```text
PENDING_DELETE/DELETE_FAILED
  --无 active Lease且领取 delete_lease_until--> DELETING
  --删除成功或不存在--> DELETED + capacity release
  --失败--> DELETE_FAILED + bounded backoff/alert
```

到期扫描若 Artifact 仍在当前 Binding，先走系统生命周期事务：完整 Adapter 锁序、解绑、revision+1、记录 invalid reason、Artifact→PENDING_DELETE；随后普通 GC 永不反向锁 Adapter。

GC 核心在领取或删除候选前调用一个不依赖具体 Lease 表、ORM 或迁移版本的删除保护 hook。B3 只实现这个接缝，并用可控 fake/stub 固定两类事实：hook 报告 protected 时不得迁移 Artifact 状态、删除 Blob 或释放 charge；报告 unprotected 时才可继续幂等删除。B3 基线没有 Lease 表，因此其证据不得声称真实 active Lease 查询或 GC 与 Execution 创建竞争已经通过。

C0 10.2 新增 Lease schema 后，以数据库 pending/running active Lease 查询实现该 hook；C0 10.3 再把统一 Execution 创建事务与 GC 放入同一真实 PostgreSQL 锁竞争，证明结果只能是“Lease 先提交并阻止删除”或“治理先提交并使 Execution 创建失败/重试”。在 C0/C4 风险 Gate 完成前，managed-files feature flag 保持关闭。该分批接线不改变长期合同：任何 active Lease 都 MUST 阻止 Blob 删除和实际容量释放。

永久删除 Adapter 前，上传创建与删除都先锁 Adapter。若存在 UPLOADING/ACTIVE reservation，返回 `adapter_upload_in_progress`。对每个已计费未删除 Blob创建 `artifact_deletion_jobs`，字段采用 Issue 给出的 storage key/SHA/size/former adapter/charged bytes/lease/status/attempt/error/capacity release 时间；在同一事务把删除责任移交、删除 Binding/Artifact/terminal reservation/InputConfig/Adapter。平台 `actual_bytes` 在移交时不变，job 完成后以 `capacity_released_at IS NULL` 扣减一次。

只依赖 FK cascade 的备选方案会先删除唯一 storage key，造成不可回收 Blob；同步删除 Blob 的备选会把用户请求绑定到不可控文件系统延迟，均否决。

### 9. Execution snapshot 与 Lease 是不同安全目的

Execution 新字段分三类：

- 审计：`input_source_type/input_config_revision/input_snapshot/error_code`；
- deadline：`timeout_seconds_snapshot/recovery_grace_seconds_snapshot/workspace_cleanup_*_snapshot/claim_deadline_at/execution_deadline_at`；
- 协议与清理：`claim_token_hash/cleanup_receipt_token_hash/workspace_cleanup_status/workspace_cleanup_error_code`。

`execution_input_artifact_leases` 含 execution/artifact/ordinal、`created_at` 和索引/约束，负责运行授权、GC 保护与最小审计时间事实；公开 snapshot 不含 Artifact ID。Execution 业务终态与 Lease 释放在同一事务，历史保留 snapshot 而不保留可操作 Lease。

Lease 表和数据库-backed 删除保护 provider 均属于 C0 公共 schema/API；Wave B 的 B3 不创建、引用或模拟这张表，只证明 GC 会正确遵守抽象保护判定。真实 Lease 保护、统一 Execution 创建原子性及两者与 GC 的竞争必须以 C0 及后续 Compose 风险 Gate 为准，不能复用 B3 hook 单测作为通过证据。

只用 snapshot 授权下载会让历史详情包含可猜测引用；只用 Lease 审计则 Blob 删除后丢失展示事实，因此二者不能合并。

### 10. Schedule 阻塞是持久 cursor 事件

`adapter_schedules` 增加 `last_blocked_reason/last_blocked_at/last_processed_due_at`。Scheduler 锁定 Schedule 后调用统一 resolver；结构性 `input_invalid` 不创建 Execution，记录当前 due point并直接把 `next_run_at` 算到数据库当前时间后的第一个 Cron 点。修复路径要求停用、保存、再启用，重基准未来 cursor；不复用当前“瞬时 Worker 离线/busy 时最多补最近点”的路径制造热循环。

### 11. v2 Claim Token 与 Cleanup Token 权限分离

Worker 行增加 `protocol_version`，未提供为 1。v2 claim 在 Execution 行锁内执行 pending→running、设置 deadline，并生成两枚 32-byte 随机 Token；Control 只保存 SHA-256 hash。Claim Token只授权 progress/result/download；Cleanup Token只授权终态 cleanup receipt。Header 分别为 `X-DLR-Claim-Token` 和 `X-DLR-Cleanup-Token`。

原始 Claim Token 只在内存 TaskPayload；原始 Cleanup Token 为崩溃恢复所必需，唯一允许持久化在 Worker 私有 journal。HTTP access log、domain log、审计、Execution schema、浏览器、URL 都先做字段级排除；错误只记录 stable code 和 execution/worker ID。

沿用平台 Worker Token 的备选方案不能证明某个 Worker 拥有某个 Execution；复用一枚 Token 做清理会让崩溃恢复凭据同时拥有下载/改写 Result 权限，违反最小权限。

### 12. Worker Workspace 采用可恢复的确定路径与外部 journal

Workspace 位于 `${DLR_RUNTIME_ROOT}/workspaces/dlr-exec-<execution-id>`，依赖环境仍在 version-scoped 目录。创建前原子写 `${DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT}/<execution-id>.json`：受控路径、Execution ID、Cleanup Token、协议版本；journal 不含 Claim Token、文件名、input、Secret 或 output。

Workspace 含 `.dlr-execution-workspace` 归属标记、`input_manifest.json`、`input/`、`temp/`、代码/harness、input/runtime config/output。下载全部成功并校验后才启动子进程；mount name由 ordinal+受控扩展名生成。同步清理实现单次 timeout、总 deadline、有限退避和存在性确认，不再用 `ignore_errors` 作为成功证据。

Result confirmed completed 后删 journal；deferred、响应丢失或 Worker 崩溃时保留。启动/周期扫描以 journal + marker + manifest 三重匹配删除；目录不存在也视为本地清理完成并幂等回执。版本依赖缓存不在扫描范围。

随机 tempfile 的备选方案在重启后难以证明归属，且 cleanup token若放 Workspace 会随目录删除而丢失，因此否决。

### 13. 三语言 Context 由同一 manifest 生成

Worker 写统一 `input_manifest.json`，Python/Node/Java harness 解析后构造等价 `InputFile` 列表。path 是当前 Worker Workspace 内的受控绝对路径；original name只用于显示/业务选择，ordinal 保持输入配置顺序。JSON `input.json` 仍写原值：none/managed_files 为 null，json 为原始 JSON。

不把文件包装进 `input`，因为这会破坏现有 Adapter。也不把文件作为环境变量，避免大小、编码与泄露问题。

### 14. Control stale reconciler 与 Worker cleanup receipt 解耦业务终态

Control 新 background loop 分批 `SKIP LOCKED` 扫描：

- pending 超 claim deadline：failed/worker_unavailable、cleanup completed、释放 Lease；
- running 超 execution deadline + grace：Worker healthy → timeout；offline → failed/worker_lost；两者 cleanup deferred/unknown、释放 Lease。

同一终态事务写 `ended_at/error_code/cleanup fields`。现有 Result idempotency扩展为不覆盖任何终态；cleanup receipt只允许 terminal 的 deferred→completed、completed→completed。业务 succeeded 加 cleanup failed仍是 succeeded。

只等待 Worker finally 的备选无法覆盖断电；在 cleanup 完成前保持 Lease 会导致永久阻塞 GC，因此 Control 必须在稳定业务终态释放 Lease，Worker 副本由 journal/reconciler独立收敛。

### 15. Web 状态由 InputConfig resource 驱动

`TaskRunSettingsPanel` 拆出输入对象子组件/状态 hook，但保持现有单页工作台结构。公共类型增加 InputConfig/Artifact/ManagedSettings/Execution snapshot；API 增加配置、设置、staged、delete，以及独立 upload client。文件卡片只有 flag 和服务端 capability 都允许时可选。

草稿模型区分 `savedRevision`、`draftSource`、已绑定 READY、会话 STAGED 与 upload progress。保存成功才替换 saved state；409保留草稿。Schedule run-now 调用 `createExecution(adapterId,{})`；dirty 时明确提示使用已保存配置，不发送 override。

Ant Design 固定 5.29.3；使用现有 Card/Form/Tooltip/Upload 展示模式，但上传 transport 由自定义 request 接管。CLI 固定版本查询已用于约束规划，实施前仍须按仓库 skill 对实际使用的组件查询 `info/demo`，不得从本设计猜 prop。

### 16. 稳定错误码集中定义并分层映射

Control 定义单一 machine code registry，Pydantic/domain/Worker report复用；Worker i18n只映射平台文案，Web user-message/i18n映射两种语言，HTTP 状态保持一致：

- 409：revision/runtime/upload in progress/quota or low-watermark concurrency conflict类；
- 413：确定的单文件大小超限；
- 422：source不可用、输入无效、旧 override、Token/cleanup transition/protocol payload校验类；
- Worker/Execution终态 `error_code` 使用 Issue完整 code 集，不以自由文本作为控制流。

安全相关响应不回显请求 filename/path/token；Control/Worker日志对 Header和payload做字段级排除，并用值扫描测试证明不泄露。

### 17. Round 1 审计修复保持一个领域事务和服务器权威

审计确认旧 Schedule PUT 不能继续调用只更新 JSON 列的通用 setter。兼容写入与新 InputConfig API 复用同一个“不提交事务”的领域 helper：由外层调用者负责 Adapter/Schedule/InputConfig/Binding/Artifact 锁和最终 commit。显式 `input:null` 与省略字段使用请求字段集合区分；切换 `managed_files→json` 时，Binding 释放、READY Artifact 进入 `PENDING_DELETE`、revision 与旧列镜像必须原子发生。不得从 Schedule service 调用会自行 commit 的公开 API。

上传 reservation 的 TTL 是 writer 存活租约，不是配额扩容副作用。流式循环用 monotonic deadline 周期调用服务端内部 renew；对外公开 renew 路由若没有真实会话调用者则删除，避免出现不可使用的伪 API。续租失败与过期走同一上传中止/补偿路径。

服务端 capability 是 Web retention 与编辑状态的唯一权威：`allow_manual_delete`、`max_custom_retention_seconds`、Runtime Lock、READY 删除 revision 均从响应/请求合同传递。Schedule enabled 只锁定当前输入写入，不禁止上传产生 STAGED；Web 必须保留未选择 STAGED 并在 SPA/浏览器离开时提示。

### 18. 删除、Scheduler 和 Worker 失败事实必须可恢复且可观测

Artifact 与 Adapter deletion job 统一使用大写状态与明确尝试字段，但仍是两个责任载体。连续删除失败达到部署阈值后停止热循环、产生管理员可见告警；显式 retry 只把符合条件的 `DELETE_FAILED` 重新排队，不清除容量 charge，不绕过 Lease。由于功能尚未发布，修正现有 Issue #127 migration 比叠加兼容错误字段的新 migration 更可控；仍必须重跑 fresh/fixed-baseline migration Gate。

Schedule 继续以顶层 `input_invalid` 作为稳定机器分类，并增加结构化 detail 保存具体原因；不把具体原因替换顶层分类。数据库 `IntegrityError` 仅把已知 active-Execution 唯一约束映射为业务冲突，其他完整性错误必须重新抛出并由测试暴露。

Worker mount name 保留 ordinal 并增加白名单扩展名，从而让 Adapter 常用解析库仍能识别文件类型；原始文件名永不参与路径。v2 payload 在任何 journal/Workspace/进程副作用前校验。journal 落盘后，Workspace mkdir 与最小 marker/manifest 建立构成一个可恢复步骤，关闭“目录已存在但三重匹配永远不成立”的崩溃窗口。Worker client 与文档只使用 canonical workspace-cleanup 路由，deferred Result 必须携带稳定 cleanup error code。

### 19. 审计裁决与同 PR 伴随功能分界

接受并实施会影响正确性、恢复性或已发布能力的 finding；对只指出证据过期的 finding，不把旧证据改名复用，而是在新 Candidate 上重跑。Lease `created_at` 属于 Issue 明确合同并保留；公开 Execution snapshot 仍不得扩展 Artifact ID、storage key 或其他可操作内部键。以下不作为代码缺陷实施：已由锁序排除的不可达死锁不新增防御分支；没有复现证据的 1280 overflow 不做猜测式布局改写；报告中已经诚实披露的 partial evidence 不改写为 PASS。

AI XLS/XLSX/Managed Input prompt 与登录 locale 得到用户明确同 PR 授权，但不写入本 change 的验收矩阵；它们由 `issue127-co-delivered-ai-login-followups` 独立规定。最终 Candidate 同时跑两个 change 的 OpenSpec/Gate，Issue #127 的 retained-app 人工验收仍保持单独未完成。

### 20. Round 2 审计修复收紧协议、权威来源与恢复上限

所有新建 Task Execution 的 Workspace cleanup 初始状态为 `pending`；Control 在 stale pending、Result 或 cleanup receipt 路径将其收敛为 `completed/deferred`。v2 Worker payload 的 Execution/Adapter/Version ID、Cleanup Token、timeout snapshot 与全部 input descriptor/mount 必须在 journal、Workspace 和进程副作用前完整验证，null snapshot 不作为合法 v2 值。cleanup receipt 只保留 Cleanup Token canonical 路由，不恢复未使用的 Worker ID 授权分支。

上传 writer 的 reservation 续租由 event-loop monotonic 周期驱动，续租失败进入既有补偿路径。浏览器恢复合同只列出服务器端 `STAGED` Artifact；不保留无真实会话调用者的 active upload-session recovery API。受控扩展名由后端公共模块单一生成，上传、Worker mount 与 Python/JavaScript/Java harness 不得各自维护白名单。

删除连续失败告警阈值使用部署配置；Schedule 顶层原因固定为 `input_invalid` 并在 detail 保存具体 code/params；Webhook 只映射已知 active-Execution 唯一约束。Web 在 capability 不可用时禁用 retention 并提供重试，不以硬编码上限降级；所有 READY/STAGED 合并最多选择八个，溢出的 STAGED 仍可见且可删除；Execution 日志下载名包含 Execution ID。

恶意 XLSX fixture 已稳定复现 central directory 的短声明大小与 CRC 可隐藏超大 deflate 流；因此在 XML 解析前对 stored/deflated member 的原始压缩流执行有界解压，并复核 CRC 与实际尺寸。所有旧 exact-SHA、迁移和 retained-app 证据在新代码冻结后视为过期，必须如实保留历史失败/partial 事实并生成新 Candidate 证据。

### 21. Round 3 审计修复统一重试门禁、协议类型与 Web 能力状态

Artifact 和 detached deletion job 的连续失败阈值是自动、业务与管理员入口共同遵守的领域门禁：未达阈值时只走有限 backoff，管理员不得提前释放；达到阈值后自动 claim 与普通业务删除都停止，只有管理员 retry 可把原状态机重新排队一次，尝试次数和容量 charge 不重置。业务端点可以跳过未达阈值的普通 backoff，但不能冒充管理员治理。

TaskPayload `protocol_version` 只接受真实整数 `1/2`；缺失或显式 null 保持 v1 兼容，bool、float、数字字符串和未知整数在任何本地副作用前拒绝。v2 的 `language/code` 必须非空白；`requirements` 必须是字符串但允许空串表示无外部依赖。报告要求把空 requirements 一并拒绝与 Version/Runtime 既有合同冲突，不接受该部分建议。

Managed Input capability 增加有序 `allowed_extensions`，Web 的 Upload accept、选择前校验和双语格式提示全部从该字段派生；服务端仍是权威校验。capability 与 STAGED list 使用分开的失败状态：列表瞬时失败不清空 capability、retention、已知 STAGED 或用户草稿；只有当前来源为 managed_files 且 capability 未知/失败/关闭时阻止保存，none/json 不被连带阻断。Schedule enabled/active 时“新增上传可形成 STAGED、保存/替换当前 Binding 仍锁定”是明确规格，因此不接受审计要求在锁定期开放替换入口。

公开 Execution snapshot 顶层键固定为所有来源共有 `source_type/revision`，仅 managed_files 额外包含 `artifacts`，不得加入可操作标识。Round 1–3 期间产生的 D3/E0/E1 dirty-tree、旧栈和 partial 收据只保留为 historical/superseded，不得继续标记为当前 Candidate exact-SHA 或 APP_READY；冻结后的最终 Gate 与人工验收仍分别由 22.13、23.8、20.2/20.3 跟踪。

### 22. PR 评论修复封闭上传资源与 Execution 生命周期边界

上传完成与请求取消共享 reservation/Artifact 锁定状态机，但文件删除权不能只由外层协程是否收到返回值决定。abort 返回显式 outcome：它可以为自己完成的 `ACTIVE/UPLOADING→CANCELLED/DELETED`，以及已经处于 `CANCELLED/EXPIRED + DELETED` 的幂等重试授权清理；观察到 `CONSUMED` 或 `STAGED/READY/删除治理状态` 时禁止删除正式对象。数据库补偿本身失败时最多清理 `.part`，正式随机对象留给 TTL/orphan audit，优先避免形成已计费 STAGED 元数据但 Blob 缺失。

自实现 multipart reader 在应用层维护累计接收预算，并在把 chunk 追加到 buffer 前检查；Header 即使与结束分隔符同 chunk 也按实际位置限长。文件 body 继续使用数据库 `max_file_bytes` 与 reservation 作为权威，framing 总预算由该值加固定余量推导；普通字段和 epilogue 使用固定小预算，第二个文件在 Header 后立即拒绝。此次不增加动态设置或 wall-clock timeout，避免为安全修复扩大配置面并误伤合法慢速大文件。

Adapter stop-delete 对 pending Execution 复用既有 Lease release helper，在同一删除事务中先完成取消与 Lease 释放，再准备 Artifact deletion job；running 分支仍只请求取消并保留 Lease。`ON DELETE RESTRICT` 不改为 CASCADE，因为它继续承担阻止误删活动输入的安全边界。

Execution 创建抽取窄的生命周期初始化 helper，统一数据库 `created_at`、timeout/recovery/cleanup 快照、cleanup pending 与 claim deadline。Task 路径在 resolver 得到输入材料后使用 helper，Webhook 保留自己的鉴权、JSON body 和 source/revision 语义后使用同一 helper，不把 Webhook ingress 送入 Task 兼容解析。

Worker claim 与 stale reconciler 使用相同 effective deadline：显式字段优先，历史 NULL 退化为 `created_at + 当前部署 claim timeout`。candidate SQL 先筛 `deadline > DB now`，行锁后重新读取 `clock_timestamp()` 并在任何 running/Worker/Token 写入前终检；过期候选仅跳过并交给 reconciler，不能混入 Lease rejection 的 409。两层门禁关闭查询与加锁之间的边界竞争。

## Risks / Trade-offs

- [LocalFileArtifactStore 只有单 Control writer] → Compose/启动 Gate、健康与文档明确限制；未来多 Control 必须先实现共享一致性 store，不静默放宽。
- [Blob rename 与数据库提交仍不是跨介质原子] → 固定先 Blob 后 STAGED，失败补偿 + TTL + orphan audit；任何对象删除均按随机 key且幂等。
- [平台容量单例行会成为上传串行点] → 上传开始/扩容/完成事务保持极短，文件流不持锁；第一阶段自托管规模优先正确性，记录锁等待指标供后续分片决策。
- [系统生命周期转换可在 Schedule enabled 时修改输入] → 仅独立系统权限路径、完整锁序、revision+1与审计；活跃 Execution 由 Lease 保持不变。
- [B3 hook 测试被误报为真实 Lease 并发证明] → B3 证据只覆盖 schema-independent protected/unprotected 接缝；C0 使用真实 Lease schema/provider 与 PostgreSQL 锁竞争，C4/E0 再以 Compose 回归，完成前 feature flag 保持关闭。
- [过早释放 Lease 与 Worker 残留副本并存] → Control仅在稳定终态释放，Worker journal/cleanup receipt处理副本；两种状态分栏观测，不把 cleanup 失败改写业务结果。
- [v1/v2 双协议扩大短期复杂度] → 协议兼容与 managed-files flag 独立，48小时零 v1 claim + active排空后收敛到 v2，兼容代码标注有期限且有指标。
- [保留 manual_delete 可能长期占满磁盘] → 仍受配额、管理员/用户删除与治理；“永久”文案明确不是不可删除。
- [用户文件名与文件内容具有敏感性] → DB/API只保存必要展示元数据，路径随机，历史无下载；普通/审计日志不记录内容、storage key、Token或路径。
- [迁移镜像造成双写分叉] → 兼容期所有旧/新 JSON写入进入同一受锁服务，迁移与运行时一致性检查遇到冲突即失败；旧列永不作为 Scheduler运行真值。
- [UI 自动 Gate不能证明审美/业务接受] → 双语视口、交互、console/request/overflow作为机器证据，保留隔离 app 给用户最终 PASS/FAIL。

## Migration Plan

1. **Wave A / expand schema**：新增 InputConfig、Execution/Schedule兼容字段与 `legacy_input_compat_enabled=true`；从固定基线回填 Schedule json、manual `json/{}`、revision=1，输出计数并对冲突 fail-fast。部署新 Control，继续 v1 Worker和旧字段镜像。
2. **Wave A / unified JSON**：manual/Scheduler/run-now 改用 resolver；新 Web先保存 InputConfig再空 body运行，四卡片可见但 managed-files/remote disabled。连续记录旧 manual/Schedule调用指标。
3. **Wave B / storage closed**：新增 settings/capacity/reservation/artifact/binding/deletion job、LocalFileArtifactStore、上传/TTL/GC/audit与Compose Control持久卷；flag仍关闭。完成并发配额、rename补偿、Adapter删除、GC故障 Gate与 schema-independent 删除保护 hook，但不声明真实 Lease/Execution 创建竞争已验证。
4. **Wave C / execution and worker**：新增 Lease、Execution deadline/Token/cleanup字段、Worker protocol version，把数据库 active Lease provider 接入 GC hook并完成真实 GC/Execution 创建竞争；先部署 v1/v2 Control，再滚动 v2 Worker和journal持久卷；实现下载、三语言Context、同步/孤儿清理、receipt与stale reconciler，故障注入 claim响应丢失/崩溃/晚到Result/Token错误。
5. **Wave D / expose**：确认目标 Worker全v2且v1 active排空后打开 managed-files flag；部署完整上传/retention/example/history/copy UI，完成双语四视口和端到端验证。
6. **兼容收敛**：新 Web上线且外部客户端迁移后，连续48小时无旧 manual/Schedule调用再关闭 `legacy_input_compat_enabled`；连续48小时无v1 claim再把最低协议升到2。
7. **观察与旧列后续**：至少7天且每个 enabled Schedule覆盖两个真实计划点，迁移计数、Smoke、故障与回滚演练通过后，另开独立变更停止镜像并删除旧 Schedule input列。

回滚按 reverse-deploy 而非 schema downgrade：先关 managed-files flag并阻止新上传/文件Execution，等待 active终态与Lease释放；保持新版 Control以v1/v2双协议服务，按兼容顺序回滚Web/Worker；保留新表、Blob、deletion jobs和旧列镜像供恢复/治理。任何容量/删除任务不得通过回滚自动清空。
