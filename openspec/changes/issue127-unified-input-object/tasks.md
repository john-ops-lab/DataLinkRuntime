# Issue #127 实施批次合同

## 0. 批次 DAG、资源与集成纪律

```text
A0 → A1 → A2 → A3
                ↓
B0 → B1 → ┬→ B2 ─┬→ B4
          └→ B3 ─┘
                    ↓
C0 → ┬→ C1 → C2 ─┬→ C4
     └→ C3 ──────┘
                    ↓
D0 → ┬→ D1 ─┬→ D3 → E0 → E1
     └→ D2 ─┘
```

- `depends_on` 是硬门禁；前序批次未形成已验证 Candidate 时不得开始后序。
- 允许并行的只有 `B2/B3`、`C1/C3`、`D1/D2`，且必须使用独立 worktree、独立 PostgreSQL database、独立 Compose project/volume、唯一 host ports、独立浏览器 profile 与匿名 fixture；本地 `main` 集成始终串行。
- 公共 schema/API/protocol/migration 仅由 A0、B0、C0、D0 定义；并行批次不得各自改写公共合同。每个 Wave 的集成顺序固定为编号升序，失败即停止、不勾选、不进入下一 Wave。
- PostgreSQL database 命名 `dlr_i127_<batch>_<session>`；Compose project 命名 `dlr-i127-<batch>-<session>`；容器必须带 `ao.session=$AO_SESSION_ID`；临时 ArtifactStore/journal/fixture 使用 `/private/tmp/dlr-i127-<batch>-<session>/`，只清理经本批次证明拥有的资源。
- 浏览器批次分配独立 token/account ports，默认从 `8920/9020` 起按批次递增；不得占用当前 retained app。所有测试数据、文件名、Token 与用户均使用明显匿名值。

| 批次 | Wave | depends_on | 主要冲突域 | 共享资源与隔离 | 串并行与本地集成顺序 |
|---|---|---|---|---|---|
| A0 | A | 固定基线 | Alembic head、models/schemas `__init__`、InputConfig API | 独立 migration DB，无 Compose/浏览器 | 必须首行串行，先公共 schema/migration |
| A1 | A | A0 | execution/schedule/adapter-runtime services 与 API | 独立 DB、冻结时钟/Worker fixtures | 串行；统一 resolver 先于 Web |
| A2 | A | A1 | `TaskRunSettingsPanel`、`api.ts/types.ts`、adapter/runtime i18n | 独立 Vitest 与浏览器 profile/ports | 串行；不得提前启用 managed_files |
| A3 | A | A2 | Wave A 全栈、Compose smoke | 独立 Compose project/volumes/ports | 串行 Gate；通过后才可进入 B |
| B0 | B | A3 | Alembic head、managed models/settings/config/app routers | 独立 migration DB | 串行公共存储 schema/API |
| B1 | B | B0 | ArtifactStore、upload API/service、capacity/reservation | 独立 store root 与 DB | 串行；先建立上传不变量 |
| B2 | B | B1 | InputConfig binding/retention/runtime lock | 独立 DB/fixture；不改 GC modules | 可与 B3 并行；本地先集成 B2 |
| B3 | B | B1 | TTL/GC/audit/Adapter deletion/background loops | 独立 DB/store；不改 binding API | 可与 B2 并行；本地后集成 B3 |
| B4 | B | B2,B3 | Wave B Compose volumes 与全生命周期 | 独立 Compose/store/ports | 串行 Gate；通过后才可进入 C |
| C0 | C | B4 | Alembic head、Execution/Worker protocol schema/API | 独立 migration DB | 串行公共协议先行 |
| C1 | C | C0 | Worker agent/client/executor、download/journal/cleanup | 独立 Worker root/journal/store | 可与 C3 并行；本地先集成 C1 |
| C2 | C | C1 | Python/Node/Java harness 与 manifest | 独立 runtime roots，固定 toolchains | 串行跟随 C1 manifest 合同 |
| C3 | C | C0 | Control stale reconciler/result/receipt/Lease release | 独立 DB/冻结时钟/fake Worker | 可与 C1 并行；本地在 C2 后集成 C3 |
| C4 | C | C2,C3 | v1/v2、三语言、崩溃/断网全栈 | 独立 Compose/store/journal/ports | 串行 Gate；通过后才可打开文件能力 |
| D0 | D | C4 | Web public types/API/i18n key skeleton/capability | 独立 Vitest，无页面接线 | 串行 Web 公共合同先行 |
| D1 | D | D0 | 输入对象/上传/retention/系统设置组件 | 独立浏览器 profile/ports；不改历史组件 | 可与 D2 并行；本地先集成 D1 |
| D2 | D | D0 | 历史/示例/复制组件及 clone service | 独立浏览器/DB；不改输入组件 | 可与 D1 并行；本地后集成 D2 |
| D3 | D | D1,D2 | `App.tsx` 接线、完整 i18n 与 Playwright | 独立 Compose/browser/ports | 串行 Wave D 视觉与业务 Gate |
| E0 | Final | D3 | 全仓回归、迁移/回滚/文档/smoke | 新鲜 DB + 独立 Compose + exact-SHA evidence | 串行最终机器 Gate |
| E1 | Final | E0 | retained app 人工验收 | 保留隔离 app，不清理其资源 | 只做人工交接；用户 PASS 不由机器代替 |

## 1. A0 — Wave A 公共 schema、迁移与 InputConfig API

- [x] 1.1 用现有 API 测试固定最小复现：manual Execution 接受 per-run input，省略 manual per-run `input` 时 `Execution.input` 按固定基线持久化为 JSON `null`，Schedule 独立保存 input；历史无 Schedule 的 manual Task Adapter 迁移回填仍保持 1.3 的 `json/{}` 合同。保存基线事实与红灯结果，验证测试在实现前准确失败于“唯一 InputConfig/revision”预期；A0 仅记录现状，不改动属于 A1 的 Execution 创建语义。
- [x] 1.2 新增 AdapterInputConfig 模型、枚举/check/index、GET/PUT schema 与路由，覆盖 none/json/managed_files 空集合/remote_files 拒绝和类型专属字段；验证 API 单元测试检查 200/409/422 与响应 `valid_for_run/invalid_reason`。
- [x] 1.3 编写 expand Alembic migration：回填 Schedule JSON、manual `json/{}`、revision=1，新建 Task 默认 none，增加 Schedule blocked 字段并保留旧 input 列；验证 fresh head、从 `896f715` 对应 schema 升级、重复回填计数一致。
- [x] 1.4 对重复/孤儿 Schedule、既有 InputConfig 冲突和非法 source 构造迁移 fixture；验证 migration fail-fast 输出 Adapter ID、source 计数与冲突数且不静默选择。
- [x] 1.5 集中定义 InputConfig 与兼容期稳定错误 code/API 响应，验证 schema 快照不包含 Artifact storage key、路径、Token 或文件内容。
- [x] 1.6 运行 A0 Gate：`uv run --frozen --project backend pytest <A0 migration/input-config tests> -q`、`uv run --frozen --project backend ruff check <changed backend files>`、`ruff format --check`、`mypy` 相关模块；全部 PASS 才形成 A0 Candidate。

## 2. A1 — Wave A 统一 resolver、Manual/Schedule/run-now 与兼容门禁

- [x] 2.1 实现受锁统一 resolver 与 Execution 创建事务，保持 `Execution.input` 原始 JSON/null 并固定 source/revision/snapshot；验证 manual 与 scheduler 针对 none、object/array/scalar/null 产出相同快照合同。
- [x] 2.2 将 `POST /api/adapters/{id}/executions` 改为读取已保存配置并支持 schedule run-now `trigger=manual`；验证 run-now 不改变 enabled/Cron/timezone/next_run_at/last_processed_due_at。
- [x] 2.3 实现 `legacy_input_compat_enabled`：开启时旧 per-run input 仅作用本次 Execution且记录弃用指标，关闭时任何 `input` 字段（含 null）返回 `execution_input_override_not_supported`；验证两种开关与输入大小门禁。
- [x] 2.4 让旧 Schedule PUT 与新 JSON InputConfig 在兼容期同事务双向镜像，Scheduler 只读统一 resolver；验证并发 revision conflict 不产生双写分叉。
- [x] 2.5 实现 Schedule input_invalid 持久阻塞与 future cursor 推进；用冻结数据库时钟、多 scheduler、Cron/timezone/DST fixture 验证不创建 Execution、不热循环、不补跑。
- [x] 2.6 将启用 Schedule、manual/run-now、保存输入接入现有 Adapter Runtime Lock和 active unique index；验证 stale 页面、enabled Schedule、pending/running 竞争均返回权威 409且失败不改 revision。
- [x] 2.7 扩展 Adapter clone：复制 source/json/retention，managed_files 副本为空且 Schedule disabled；验证不复制任何 Artifact/Binding/Lease 标识。
- [x] 2.8 运行 A1 Gate：统一 resolver、schedule、execution、clone、锁竞争和兼容开关测试，以及相关 Ruff/format/mypy；执行兼容开关 on→off→on 回滚 smoke，全部 PASS 才形成 A1 Candidate。

## 3. A2 — Wave A none/json Web 与四卡片占位

- [x] 3.1 按 antd 5.29.3 固定 CLI 查询实际使用的 Card/Form/Tooltip API/demo并记录结果；验证未更改 `package.json` 与 antd/pro-components 锁定版本。
- [x] 3.2 在 Task 运行设置建立独立 Input Object 草稿/revision/save 状态，移除 manual/Schedule 两套 JSON 编辑路径；Vitest 验证切换 run_mode 不丢草稿且保存失败保留最近有效配置。
- [x] 3.3 实现 none/json 四种顶层 JSON 保存和语法校验，Execution/run-now 只发送空 body；验证 object/array/scalar/null、dirty草稿提示和 `input` 字段永不由新 Web 发出。
- [x] 3.4 展示四张可聚焦卡片：remote_files 永久“开发中”，managed_files 在 flag 关闭时“尚未启用”；验证禁用状态不调用上传/文件 API且后端绕过请求仍稳定拒绝。
- [x] 3.5 补齐 zh-CN/en adapter/runtime key、错误映射与插值一致性；运行 i18n key/placeholder Vitest，验证无 raw key和非本地化 machine code。
- [x] 3.6 运行 A2 Gate：`npm run test -- <focused files>`、`npm run lint`、`npm run typecheck`、`npm run build`；在独立浏览器以 zh-CN/en、1280/1920 验证 none/json、revision conflict、run-now、console/request/overflow。

## 4. A3 — Wave A 迁移、回滚与集成 Gate

- [x] 4.1 在新鲜 DB 与固定基线 DB分别演练 Alembic head，核对 Adapter 总数/source 分布/冲突数、历史 Execution input 未改；验证重复 upgrade/check 无副作用。
- [x] 4.2 用旧 Web 风格 manual/Schedule 请求和新 Web请求执行兼容矩阵；验证开关开启无静默分叉、关闭后两类旧字段均明确拒绝、重新开启可回滚。
- [x] 4.3 在独立 Compose 执行 manual、Schedule、schedule run-now，验证单活跃门禁、invalid cursor推进、服务日志脱敏与四卡片 flag off。
- [x] 4.4 运行 Wave A 完整 backend/web 相关回归与 `git diff --check`，归档 exact-SHA、命令、PASS/FAIL、迁移计数、浏览器截图/console/request证据；任何失败不勾选、不进入 B。

## 5. B0 — Wave B Managed Settings、容量与 Artifact 公共 schema

- [ ] 5.1 固定配额超卖、低水位、retention 追溯重算和 null/无限设置的最小红灯 fixture；验证实现前无法满足 settings/usage 合同。
- [ ] 5.2 新增 ManagedInputSettings、platform capacity、reservation、Artifact、Binding、deletion job 模型及全部 PK/FK/复合 FK/unique/check/index；用 PostgreSQL catalog 测试验证约束和 GC/TTL 查询索引。
- [ ] 5.3 新增管理员 GET/PUT settings API、初始值/范围/跨字段不变量与 usage/over_quota 响应；验证普通用户拒绝、降配额不删现有文件且减少占用操作仍允许。
- [ ] 5.4 新增 `DLR_ARTIFACT_STORE_ROOT`、feature flag、GC/audit intervals 与有界启动校验；验证数据库设置不返回/覆盖部署路径，非法环境配置阻止启动。
- [ ] 5.5 编写 B0 migration与 fresh/upgrade schema测试，验证种子单例、capacity 计数初始一致、回滚只作为测试清理而不定义生产破坏性 downgrade。
- [ ] 5.6 运行 B0 targeted pytest、Ruff/format/mypy和 migration catalog Gate；全部 PASS 才允许 B1。

## 6. B1 — Wave B LocalFileArtifactStore、上传与 reservation

- [ ] 6.1 用临时同/跨文件系统、symlink、`../`、伪 Content-Length、上传中断和并发最后配额构造最小复现；验证红灯精确覆盖原子 rename/路径/预留风险。
- [ ] 6.2 实现无第三方依赖的 LocalFileArtifactStore 窄接口、随机 storage key、同挂载 `.part→object` 原子 rename、幂等 delete/stat/quarantine；文件系统测试验证路径穿越和 symlink拒绝。
- [ ] 6.3 实现 multipart 流式 upload API：Adapter权限、白名单、大小/SHA-256、低水位、reservation创建/续租/扩容/核销与失败补偿；验证不信任 MIME/Content-Length且超限立即停止。
- [ ] 6.4 实现 staged list/delete API和 upload session恢复；验证刷新只看到同 Adapter STAGED元数据、跨 Adapter猜测不泄露存在性、STAGED删除不改 revision。
- [ ] 6.5 实现 reservation TTL与 writer竞争的条件状态更新；并发/故障注入验证 ACTIVE只能单向 terminal、reserved/actual bytes不重复释放或提前释放。
- [ ] 6.6 扫描 API/日志/审计响应，验证不出现 storage key、store root、`.part`路径、文件内容或认证凭据，并验证稳定错误码全集中的上传子集。
- [ ] 6.7 运行 B1 unit/integration/concurrency/fault tests及 Ruff/format/mypy；使用独立临时 store演练中断后无无主对象或由 audit可治理，PASS才形成 Candidate。

## 7. B2 — Wave B Binding、retention 与系统生命周期事务

- [ ] 7.1 实现 managed_files 0..8 原子保存：锁序、expected_revision、所有权/READY-STAGED/ordinal/同名NFC casefold校验、Binding全替换、revision+1；并发测试验证旧 revision零副作用。
- [ ] 7.2 在保存事务中具体化 system_default/custom/manual_delete retention，并重算当前集合而不追溯已移除文件；冻结数据库时钟验证服务端 `expires_at` 权威和管理员默认变更不回写。
- [ ] 7.3 实现显式替换和 source切换：新 STAGED→READY、旧 READY→PENDING_DELETE，保存失败保留 STAGED；验证绝不原地覆盖 Blob。
- [ ] 7.4 接入用户 Runtime Lock与系统 lifecycle专用路径；锁竞争测试验证 enabled/active拒绝用户写入，而到期/损坏治理可在完整锁序下解绑、revision+1且保留 active Lease。
- [ ] 7.5 验证 managed_files 空集合保存成功但 run/schedule enable返回 `input_invalid/managed_files_empty`，1..8 READY未过期可运行，过期/非READY返回结构化 reason。
- [ ] 7.6 运行 B2 binding/retention/lock/expiry targeted tests及静态 Gate；生成锁序并发证据后才形成 Candidate。

## 8. B3 — Wave B TTL、GC、删除任务、Adapter 删除与审计

- [ ] 8.1 实现 UPLOADING/STAGED TTL领取与 Artifact `PENDING_DELETE/DELETE_FAILED→DELETING→DELETED` 删除租约、有限退避和告警；冻结时间/崩溃测试验证 stale DELETING可重领且对象不存在算成功。
- [ ] 8.2 实现 active Lease删除保护和实际容量一次释放；并发测试验证 GC与Execution创建竞争时要么先建Lease要么先治理，永不删除运行所需Blob。
- [ ] 8.3 实现当前 Binding到期的系统生命周期转换与低频 orphan audit；验证 audit只隔离合法随机、超过宽限、无Artifact/job记录对象，不碰未知目录。
- [ ] 8.4 扩展 Adapter delete：与上传创建都先锁 Adapter，UPLOADING/ACTIVE时409；原子把已计费 Blob和charge转入独立 deletion jobs，再删除元数据。
- [ ] 8.5 对 deletion job重复消费、对象缺失、删除失败/重启注入故障；验证 `capacity_released_at`只写一次、平台charge不在Adapter事务中丢失。
- [ ] 8.6 审计上传/绑定/替换/删除/管理员治理和GC失败，验证主体/Adapter/Artifact/stable code可观测且文件内容、Token、storage key、宿主路径脱敏。
- [ ] 8.7 运行 B3 TTL/GC/adapter-delete/audit并发与故障测试、静态 Gate；证明只清理本批次临时 store 后形成 Candidate。

## 9. B4 — Wave B 全生命周期与 Compose Gate

- [ ] 9.1 更新 Compose为Control ArtifactStore持久卷和全部B阶段环境变量，保持 managed_files flag off；`docker compose config -q`验证默认/覆盖值与单Control边界。
- [ ] 9.2 在隔离 Compose 上传 allowed/blocked/oversize 文件、刷新恢复、保存0/8、替换、到期、删除、配额下降/恢复和低水位；验证DB/Blob/charge状态逐步一致。
- [ ] 9.3 故障注入上传中断、rename后DB失败、reservation TTL竞争、GC崩溃、delete失败与Adapter删除竞争；重启Control后验证最终收敛且无静默残留。
- [ ] 9.4 执行B阶段 rollback演练：flag保持关闭、停止后台loop后再启、保留表/Blob/job并恢复治理；验证不做schema downgrade或自动删数据。
- [ ] 9.5 运行 Wave B backend、fresh/upgrade migration、Compose smoke和日志敏感值扫描，归档 exact-SHA/资源清单/证据；任何失败不进入 C。

## 10. C0 — Wave C Execution/Lease 与 Worker v1/v2 公共协议

- [ ] 10.1 固定最小红灯：配置替换影响pending文件、v1领取文件、无Token下载/Result、stale Execution永久Lease；验证测试在C0前精确失败。
- [ ] 10.2 新增 Execution snapshot/deadline/token-hash/cleanup字段、Worker protocol_version与Lease表约束/索引；migration测试验证fresh/upgrade、nullable v1兼容和历史Execution不被改写。
- [ ] 10.3 扩展统一Execution创建事务以固定timeout/claim/recovery/cleanup快照和文件Lease；验证 attempt<=total<grace、数据库时钟deadline、后续配置/GC不改变pending/running。
- [ ] 10.4 扩展Worker register/claim/TaskPayload与最低协议门禁：缺失=1、v1仅none/json、v2文件任务；验证mixed pool不让旧Worker领取不可完成任务。
- [ ] 10.5 在v2 claim行锁事务生成32-byte Claim/Cleanup Token、只存hash并定义Header校验依赖；constant-time/hash/API schema测试验证两类Token不可互换且不进入公开响应。
- [ ] 10.6 新增Worker内部下载和cleanup receipt协议schema/路由骨架，锁定stable code与HTTP合同；契约测试验证非Worker入口和非法Header拒绝。
- [ ] 10.7 运行C0 migration/protocol/lease/contract targeted tests及Ruff/format/mypy；公共协议通过后才允许C1/C3并行。

## 11. C1 — Wave C Worker 下载、journal 与同步/延迟清理

- [ ] 11.1 实现Control内部下载授权：Worker ownership、running状态、Claim Token、active Lease、Artifact可读与metadata/content一致；越权/猜测/终态测试验证不泄露对象存在性。
- [ ] 11.2 扩展Worker client对download/progress/result携带Claim Header、cleanup receipt携带Cleanup Header；单元测试验证Token不进URL/body/log且v1请求保持旧合同。
- [ ] 11.3 将Workspace改为受控确定路径，在创建前以0700目录/0600文件、fsync+atomic rename写外部journal；故障注入journal失败验证不创建目录、不下载、不启动Adapter。
- [ ] 11.4 下载全部输入到受控mount name，流式复核size/SHA-256并写marker/manifest、0444/0555；验证任一下载/校验失败不启动子进程且进入清理。
- [ ] 11.5 实现同步清理attempt/total硬预算、有限退避/存在确认与Result cleanup字段；挂起删除注入验证总时长有界、业务成功不被cleanup失败覆盖。
- [ ] 11.6 实现journal启动/周期扫描与幂等receipt：只删名称+marker+manifest三重匹配Workspace，不删version依赖/未知目录；重启/响应丢失测试验证completed→completed后才删journal。
- [ ] 11.7 对Worker/Control日志和journal做敏感值扫描，验证Claim Token从不落盘、Cleanup Token仅在私有journal，用户文件名/input/Secret/output不入journal。
- [ ] 11.8 运行C1 Worker unit/integration/fault/timeout tests与静态Gate，在独立runtime/journal根证明可恢复清理后形成Candidate。

## 12. C2 — Wave C Python/JavaScript/Java Context 文件 API

- [ ] 12.1 固定三语言manifest/InputFile相同fixture，验证现有Context缺少文件API的红灯且JSON `handle(context,input)`行为作为retained assertion。
- [ ] 12.2 Python harness实现`context.input_files`与InputFile元数据，验证ordinal/path/original_name/content_type/size_bytes/sha256和managed_files时input=null。
- [ ] 12.3 Node harness实现等价`context.inputFiles`/camelCase字段，验证读取文件内容、顺序与JSON输入不包装。
- [ ] 12.4 Java Runtime实现等价`context.inputFiles`不可变列表/字段，使用Java 21真实编译运行验证文件和JSON合同。
- [ ] 12.5 为非法manifest、mount路径越界、文件缺失/篡改加入三语言拒绝测试；验证Adapter未启动或稳定失败且Control路径不暴露。
- [ ] 12.6 运行`backend/tests/test_multilang_runtime.py`相关完整三语言测试、Ruff/format/mypy；文档测试验证0444/0555仅称best-effort防误写。

## 13. C3 — Wave C stale Execution、晚到 Result 与 cleanup receipt

- [ ] 13.1 实现分批`SKIP LOCKED` stale reconciler：pending超claim deadline→failed/worker_unavailable+cleanup completed+Lease释放；冻结时钟/多Control测试验证单次收敛。
- [ ] 13.2 实现running超deadline+grace按Worker健康进入timeout或failed/worker_lost，cleanup deferred/unknown并释放Lease；验证不自动重跑。
- [ ] 13.3 扩展Result/progress终态幂等与ownership-first校验；竞态测试验证晚到succeeded不能覆盖stale终态、非owner即使终态也拒绝。
- [ ] 13.4 实现cleanup receipt仅terminal deferred→completed和completed→completed；验证不改业务status/output/error/ended_at，非法转换返回`workspace_cleanup_transition_invalid`。
- [ ] 13.5 验证业务终态、cleanup字段、ended_at/error_code与Lease释放同一事务；DB故障注入证明不会出现终态已写但Lease遗留的部分提交。
- [ ] 13.6 运行C3 stale/late-result/receipt/concurrency tests、Ruff/format/mypy与loop启动/取消测试；PASS后形成Candidate。

## 14. C4 — Wave C 协议、故障恢复与 Compose Gate

- [ ] 14.1 在隔离Compose先以v1/v2 Control+v1 Worker运行none/json，再滚动v2 Worker；验证旧Worker可上报、v1 cleanup标记legacy_unverified、文件任务只给v2。
- [ ] 14.2 真实运行Python/JavaScript/Java各一个多文件Execution，验证TaskPayload无Control路径、下载hash一致、Context可读、Workspace终态删除、Blob由Lease保护。
- [ ] 14.3 故障注入claim响应丢失、下载中断/hash篡改、Progress/Result Token错误、Cleanup Token互换、Worker崩溃/断网/晚到Result和清理挂起；验证stable终态、无重跑、journal恢复与日志脱敏。
- [ ] 14.4 验证managed_files开放前门禁：目标Worker全v2、无v1 active、B/C Gate通过；条件不满足时flag/API/UI继续关闭。
- [ ] 14.5 演练Worker/Control回滚顺序：先关flag并排空active，保持双协议Control和Blob/journal，再回滚Worker；验证不丢Lease/cleanup治理。
- [ ] 14.6 运行Wave C完整backend/migration/Compose smoke、三语言与敏感值扫描，归档exact-SHA和故障证据；任何失败不进入D。

## 15. D0 — Wave D Web 类型/API/i18n 公共合同

- [ ] 15.1 固定Web最小红灯：文件flag开启后的类型/API缺失、multipart认证/CSRF、历史snapshot与clone空文件；验证focused Vitest在实现前准确失败。
- [ ] 15.2 新增ManagedSettings/InputConfig/Artifact/Execution snapshot公共类型和JSON API方法，保持upload transport独立；TypeScript契约测试验证不暴露storage key/路径/Token。
- [ ] 15.3 建立zh-CN/en输入/设置/历史/错误key与插值骨架，验证namespace leaf key、placeholder一致且machine code不本地化。
- [ ] 15.4 运行D0 focused Vitest、ESLint、typecheck；公共合同PASS后才允许D1/D2并行。

## 16. D1 — Wave D 文件输入、上传、retention 与系统设置 UI

- [ ] 16.1 按antd 5.29.3固定CLI查询实际Card/Form/Upload/Progress/Tooltip组件API/demo；验证锁定版本不变且实现不猜API。
- [ ] 16.2 实现文件卡片flag开放、独立multipart上传进度、STAGED刷新恢复/离开提示和0..8文件列表；Vitest验证Cookie/CSRF、长文件名、NFC/casefold冲突与第九文件门禁。
- [ ] 16.3 实现显式替换/删除、READY/STAGED状态和expected_revision保存；并发UI测试验证409保留草稿、STAGED删除不改revision、服务端Runtime Lock权威。
- [ ] 16.4 实现system_default/custom/“永久保留”选择及服务端expires_at展示；测试验证管理员范围/禁用manual_delete、over_quota和低水位错误。
- [ ] 16.5 扩展系统设置Managed Input策略/用量/over_quota页面，验证非空边界、超额量、只减占用操作提示且不出现部署路径。
- [ ] 16.6 运行D1 Vitest、lint/typecheck/build及独立浏览器zh-CN/en 1280/1920上传/保存/锁/错误 smoke，验证console/request/overflow后形成Candidate。

## 17. D2 — Wave D 历史摘要、示例复制与 Adapter 复制

- [ ] 17.1 实现Python/JavaScript/Java只读示例生成与显式clipboard复制；测试验证只用Context API、不写Monaco、不创建AdapterVersion。
- [ ] 17.2 扩展Execution详情按none/json/managed_files显示不可变摘要；测试验证无Artifact ID、下载/复用/恢复/再次运行入口且历史列表仍无大字段。
- [ ] 17.3 完成managed_files clone后空集合、retention保留、Schedule disabled与重新上传提示；后端/Web测试验证源Blob/Artifact/Binding/Lease绝不复用。
- [ ] 17.4 补齐D2双语文案、键盘/焦点/禁用原因；运行focused Vitest、backend clone tests、lint/typecheck/build后形成Candidate。

## 18. D3 — Wave D 双语浏览器矩阵与完整业务 Gate

- [ ] 18.1 串行集成D1再D2并完成`App.tsx`接线，解决公共i18n/API冲突；运行Web全量Vitest/lint/typecheck/build验证无回归。
- [ ] 18.2 在独立Compose开启flag，浏览器验证manual、schedule run-now、上传/刷新/替换/删除/空文件/retention/历史/示例/clone完整路径与权威请求。
- [ ] 18.3 执行zh-CN/en × 1280/1440/1680/1920矩阵，保存截图、console、request与横向overflow证据；验证键盘可达、长文件名可查看且关键操作无遮挡。
- [ ] 18.4 验证Wave B/C未满足时flag/API/UI关闭，满足全部协议排空条件后才开放；演练再次关闭flag不破坏既有Blob/Execution历史。
- [ ] 18.5 归档Wave D exact-SHA机器Gate并标记“待人工验收”，自动证据不得写用户PASS。

## 19. E0 — 最终回归、迁移/回滚演练与 exact-SHA 证据

- [ ] 19.1 对新鲜DB、固定基线、重复回填和冲突fixture运行完整Alembic验证，核对计数、历史input、旧列镜像与schema约束。
- [ ] 19.2 运行backend全量`uv run --frozen --project backend ruff check .`、`ruff format --check .`、`mypy`、`pytest`，逐项记录PASS/FAIL。
- [ ] 19.3 运行web全量`npm run lint`、`npm run typecheck`、`npm run test`、`npm run build`及目标Playwright矩阵，逐项记录PASS/FAIL。
- [ ] 19.4 在全新隔离Compose运行三语言、Schedule/run-now、配额/到期/GC、Worker崩溃/恢复、Adapter删除与敏感值扫描；验证单Control边界和所有任务资源归属。
- [ ] 19.5 完成API变更、LocalFileArtifactStore单Control、迁移/兼容/回滚运维文档并演练非破坏回滚：关flag、禁新增、排空active、保持双协议与表/Blob/job/旧列；验证文档命令可执行且恢复新版后治理继续。
- [ ] 19.6 对Candidate exact SHA运行OpenSpec strict/all strict、`git diff --check`、scope/密钥/绝对路径扫描并归档source_candidate；所有机器GatePASS才允许E1。

## 20. E1 — Retained-app 用户最终验收

- [ ] 20.1 以匿名本地值保留E0 exact-SHA隔离app，回传token/account入口、测试账号、fixture和已知边界；验证服务/浏览器健康但仅标记`APP_READY`。
- [ ] 20.2 请用户按manual/schedule/run-now、上传/retention/历史/clone和双语视觉清单判定PASS/FAIL；未收到明确PASS不得勾选或宣称业务验收完成。
- [ ] 20.3 用户PASS后单独记录human acceptance事实；若FAIL，生成新Candidate并只重跑受影响及最终Gate，旧SHA证据不得复用为新SHA通过。

## 21. Issue 验收项追溯矩阵

| 验收 ID | Issue 验收事实 | 规格 Requirement | 实施/Gate 任务 |
|---|---|---|---|
| AC-U01 | Task Adapter只有一套配置和单调revision | adapter-input-config：每个Task Adapter只有一套当前输入配置 | 1.2-1.4, 2.1, 7.1 |
| AC-U02 | manual/schedule/run-now共用解析、校验、快照、下发 | adapter-input-config：所有Task运行入口复用同一输入解析合同 | 2.1-2.2, 10.3, 14.2 |
| AC-U03 | schedule run-now为manual trigger且不改cursor | adapter-input-config：所有Task运行入口复用同一输入解析合同 | 2.2, 3.3, 18.2 |
| AC-U04 | 新建none默认且JSON顶层合同不变 | adapter-input-config：输入来源具有封闭类型合同 | 1.2-1.3, 3.3, 12.1-12.4 |
| AC-U05 | 四卡完整且remote前后端不可用 | managed-input-web：四类输入卡片遵守能力门禁 | 3.4, 18.2-18.4 |
| AC-U06 | Wave B/C前不暴露managed_files | input-compatibility-rollout：Managed Files feature flag按完整能力门禁开放 | 3.4, 14.4, 18.4 |
| AC-U07 | managed_files可存0..8、仅1..8有效可运行 | adapter-input-config：Managed Files保存态与运行态分离 | 7.1, 7.5, 16.2 |
| AC-U08 | 旧revision为409且失败不覆盖 | adapter-input-config：输入配置使用乐观并发控制 | 1.2, 7.1, 16.3 |
| AC-U09 | enabled或active时用户不可改输入 | adapter-input-config：用户输入写入遵守Runtime Lock | 2.6, 7.4, 16.3 |
| AC-U10 | 系统生命周期可按锁序失效输入 | adapter-input-config：系统生命周期转换使用完整锁序 | 7.4, 8.3, 9.2 |
| AC-S01 | 动态策略由DB单例/管理员API管理 | managed-input-lifecycle：Managed Input设置具有单一权威来源 | 5.2-5.3, 16.5 |
| AC-S02 | store root/flag/loops仅部署环境变量 | managed-input-lifecycle：Managed Input设置具有单一权威来源 | 5.4, 9.1, 19.4 |
| AC-S03 | 容量/期限设置明确非空有界 | managed-input-lifecycle：Managed Input设置具有单一权威来源 | 5.3-5.4, 16.5 |
| AC-S04 | 降配额不删现有且over_quota可观测 | managed-input-lifecycle：Managed Input设置具有单一权威来源 | 5.3, 9.2, 16.5 |
| AC-S05 | claim/grace/cleanup唯一来源与不变量 | execution-input-snapshot：超时快照有范围与不变量 | 5.4, 10.3, 11.5 |
| AC-L01 | UPLOADING/STAGED/READY明确且无伪事务 | managed-input-lifecycle：Artifact与Binding具有可验证状态机 | 6.3-6.5, 9.2 |
| AC-L02 | `.part`校验和rename后才STAGED | managed-input-lifecycle：ArtifactStore保证受控、原子且不可猜测的对象路径 | 6.2-6.3, 9.3 |
| AC-L03 | 上传完成原子核销reservation/实际容量 | managed-input-lifecycle：并发上传使用原子容量预留 | 6.3, 6.5, 9.3 |
| AC-L04 | 刷新可恢复STAGED | managed-input-lifecycle：Artifact与Binding具有可验证状态机 | 6.4, 16.2, 18.2 |
| AC-L05 | Binding/reservation/Lease/Artifact/job约束索引完整 | managed-input-lifecycle：Artifact与Binding具有可验证状态机 | 5.2, 10.2, 19.1 |
| AC-L06 | Binding原子替换、retention具体化、旧文件待删 | managed-input-lifecycle：保存绑定原子具体化retention | 7.1-7.3, 9.2 |
| AC-L07 | 未保存/中断/失败释放预留并TTL清理 | managed-input-lifecycle：TTL与GC必须幂等且可重领 | 6.5, 8.1, 9.3 |
| AC-L08 | 白名单、8文件、同名与显式替换 | managed-input-lifecycle：文件类型、名称和大小由服务端权威校验 | 6.3, 7.1-7.3, 16.2-16.3 |
| AC-L09 | 文件/Adapter/平台配额与低水位并发有效 | managed-input-lifecycle：磁盘低水位与容量记账覆盖所有占用阶段 | 6.3, 6.5, 9.2-9.3 |
| AC-L10 | GC仅删无active Lease且失败可重试 | managed-input-lifecycle：TTL与GC必须幂等且可重领 | 8.1-8.2, 9.3 |
| AC-L11 | stale DELETING可安全重领 | managed-input-lifecycle：TTL与GC必须幂等且可重领 | 8.1, 9.3 |
| AC-L12 | Adapter删除与上传按行锁串行 | managed-input-lifecycle：Adapter删除与上传创建串行化 | 8.4, 9.3 |
| AC-L13 | Adapter删除移交job/charge且只释放一次 | managed-input-lifecycle：Adapter删除与上传创建串行化 | 8.4-8.5, 9.3 |
| AC-E01 | Execution保存revision/error/超时/摘要 | execution-input-snapshot：Execution固化完整输入快照 | 10.2-10.3, 19.1 |
| AC-E02 | 文件摘要无可操作引用/路径 | execution-input-snapshot：Execution历史只暴露不可操作输入摘要 | 10.3, 17.2 |
| AC-E03 | Execution.input/handle保持原JSON | execution-input-snapshot：Execution固化完整输入快照 | 2.1, 12.1-12.4, 14.2 |
| AC-E04 | invalid Schedule不建Execution且推进未来点 | execution-input-snapshot：Schedule输入失效必须消费计划点而不热循环 | 2.5, 4.3, 18.2 |
| AC-E05 | 输入修复要求停用/保存/重启且不补跑 | execution-input-snapshot：Schedule输入失效必须消费计划点而不热循环 | 2.5, 18.2 |
| AC-E06 | 替换/删除/GC不影响既有active Execution | execution-input-snapshot：文件Execution使用运行期Lease固定具体集合 | 8.2, 10.3, 14.2 |
| AC-E07 | stale pending/running稳定终态并释放Lease | execution-input-snapshot：stale pending/running Execution在Control侧收敛 | 13.1-13.2, 14.3 |
| AC-E08 | 晚到Result不覆盖终态或重跑 | execution-input-snapshot：终态与晚到报告幂等 | 13.3, 14.3 |
| AC-W01 | 二进制不进PG、payload无Control路径 | worker-input-protocol：TaskPayload不暴露存储路径 | 6.2, 10.4, 14.2 |
| AC-W02 | Lease+Claim Token下载并复核size/hash | worker-input-protocol：Worker下载由Execution、Lease与Claim Token联合授权 | 11.1-11.4, 14.2 |
| AC-W03 | v2 progress/result/download与cleanup分权Token | worker-input-protocol：v2 Claim签发分权Token | 10.5-10.6, 11.2, 14.3 |
| AC-W04 | cleanup receipt deferred/completed幂等 | worker-input-protocol：Cleanup Receipt独立且幂等 | 11.6, 13.4, 14.3 |
| AC-W05 | Workspace前原子私有journal并可恢复 | worker-input-protocol：Workspace创建前持久化私有清理日志 | 11.3, 11.6, 14.3 |
| AC-W06 | 下载失败不启动Adapter并稳定错误 | worker-input-protocol：Worker在启动Adapter前完整准备输入 | 11.4, 14.3 |
| AC-W07 | Python/JavaScript/Java Context可读文件 | worker-input-protocol：三语言Context提供稳定文件API | 12.2-12.4, 14.2 |
| AC-W08 | 0444/0555仅best-effort防误写 | worker-input-protocol：Worker在启动Adapter前完整准备输入 | 11.4, 12.5-12.6 |
| AC-W09 | 所有业务终态尝试清理且失败保留业务结果 | worker-input-protocol：同步Workspace清理具有硬预算 | 11.5, 14.3 |
| AC-W10 | 独立receipt不修改业务结果 | execution-input-snapshot：业务结果与Workspace清理结果独立 | 13.4-13.5, 14.3 |
| AC-W11 | 启动/周期孤儿清理不删依赖缓存 | worker-input-protocol：Worker重启与周期扫描安全治理孤儿Workspace | 11.6, 14.3 |
| AC-C01 | Schedule/manual默认按规则迁移且冲突失败 | input-compatibility-rollout：基线数据迁移确定且可审计 | 1.3-1.4, 4.1, 19.1 |
| AC-C02 | manual/Schedule旧输入兼容并在关闭后拒绝 | input-compatibility-rollout：旧manual输入兼容窗口/旧Schedule双向镜像 | 2.3-2.4, 4.2 |
| AC-C03 | Worker v1/v2滚动且文件只给v2 | input-compatibility-rollout：Worker v1/v2滚动升级独立于文件flag | 10.4-10.5, 14.1, 14.4 |
| AC-C04 | per-run override拒绝且发布说明含迁移 | adapter-input-config：所有Task运行入口复用同一输入解析合同 | 2.3, 4.2, 19.5 |
| AC-C05 | Clone复制类型/JSON/retention不复制文件事实 | adapter-input-config：Adapter复制不共享文件资产 | 2.7, 17.3 |
| AC-C06 | managed_files副本为空且需重传 | managed-input-web：Adapter复制页面说明文件不复制 | 17.3, 18.2 |
| AC-C07 | 历史无下载/复用/恢复/再次运行 | managed-input-web：Execution详情按source只读展示快照 | 17.2, 18.2 |
| AC-C08 | LocalFileArtifactStore单Control边界有测试文档 | input-compatibility-rollout：Compose明确持久卷与单Control边界 | 9.1, 19.4-19.5 |
| AC-C09 | Wave A-D顺序、同分支、最终单PR | input-compatibility-rollout：Wave A-D顺序交付且不得拆成多PR | 0批次DAG, 4.4, 9.5, 14.6, 18.5, 19.6 |
