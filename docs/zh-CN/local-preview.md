# 固定入口本机预览

连续 PR 使用私有配置选定的同一环境，保留数据库、材料卷、管理员 Token 和 Master Key。控制器源码在 `tools/local-preview/`；安装目录由 `DLR_PREVIEW_HOME` 指定。仓库不记录个人部署地址、端口、路径或运行资产。

## 登记验收目标

在用户要求提交 PR 并提供本地验收环境时，登记该 PR；控制器等其当前 HEAD 的 CI 成功后自动更新。不要因为另一个 PR 更新得更晚而抢占当前目标，也不要为普通后继 PR 再开一套端口。

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" select <PR_NUMBER>
python3 "$DLR_PREVIEW_HOME/preview.py" status
```

`select` 只接受配置仓库内开放、非草稿 PR。后台按私有配置中的轮询间隔查询当前完整 HEAD 的最新 `ci.yml` pull_request run，要求整个 run 成功且 `backend`、`web`、`compose-smoke` 均成功。相同 SHA 的 CI 重跑不会重复部署。关闭或合并目标 PR 后停止更新，已有应用保留。这里不会合并任何 PR。

状态输出区分 config（期望 PR）、candidate（最近候选）、deployment（最后验证成功的 SHA/CI/备份/执行回执）、status（当前动作）与 attention（未完成切换）。`Ready` 是自动验证结果，不能替代用户验收。

```sh
# 验收时固定当前版本；已开始的切换完成后返回。
python3 "$DLR_PREVIEW_HOME/preview.py" pause
python3 "$DLR_PREVIEW_HOME/preview.py" resume
# 复制现有管理员 Token，不在输出中显示。
python3 "$DLR_PREVIEW_HOME/preview.py" copy-token
```

`pause` 会让已开始的操作完成并阻止下一次更新。驻留 watcher 无需退出；如有 active operation，等它结束后再规划。`plan-carry-forward` 从开始核验到 manifest 持久化期间会排除 watcher 操作，并让并发 `resume` 等到规划结束。

## 升级规则

候选必须包含已部署 Git 提交；当前数据库 revision 必须位于唯一、完整、未改变既有 revision/down_revision 的 Alembic 迁移链中。历史已应用迁移的函数修复允许存在，实际升级只执行当前数据库版本之后的迁移。历史分叉或无法向前迁移时记录原因，保留现场，不自动降级或另建环境。若人工重写仅涉及非运行文件，可在私有 `state.json` 登记 `history_anchor_sha`；控制器仍核对全部部署源码路径完全相同，并要求候选继承该锚点。实际运行 SHA 与镜像记录保留原值，只有完成新部署才更新。

构建发生在无宿主目录共享、无 SSH Agent 转发的 VM 内，源码使用完整 SHA 提取。宿主 GitHub 登录凭据不传给 VM。镜像采用完整 SHA 标签并记录 image ID；构建完成后再次检查目标、HEAD、最新 CI 和数据库版本，才进入切换。

切换先检查执行与清理空闲，停止 Control 的 API、调度器和重试写入，再检查一次；如有竞争中的执行，恢复旧 Control 并等待。空闲后停 Worker/Web，保存并校验 PostgreSQL custom-format 备份，再迁移数据库。升级前后比较既有适配器、版本、权限、配置、模板、凭据、材料元数据等行的哈希，允许迁移新增默认数据，但不得改写或丢失旧资产。恢复应用后验证精确镜像、私有 cgroup、健康，以及一次真实 RabbitMQ → Worker 执行和工作区清理。全部通过才更新 deployed。

数据库备份保存在 配置的 VM 根目录下 `backups/<timestamp>-<old>-to-<new>/`。资产清单仅含列名与行哈希，备份文件本身包含业务数据，目录权限受限。数据库备份不包含材料卷；部署从不删除任何卷。

### 显式 carry-forward 保全升级

默认部署仍要求执行与清理责任全部空闲。只有因已知基础设施 Incident 留下、且新版本提供向前兼容处置能力的责任，才可使用一次性的私有 carry-forward manifest。它不是 `ignore busy` 开关，也不会取消旧 Execution、改写 cleanup、释放 Admission 或删除卷。

先让普通 watcher 完成候选构建并因 busy 门禁等待，然后暂停 watcher。把明确的 Execution/Incident 选择写入权限为 `0600`、父目录为 `0700` 的私有 JSON：

```json
{
  "queued": [
    {"execution_id": 123, "incident_ids": [456]}
  ],
  "cleanup_execution_ids": [789]
}
```

ID 必须是明确正整数；不接受通配符、重复项、空选择或客户端自称的 cleanup 分类。示例 ID 只是结构占位，不对应任何环境。

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" pause
python3 "$DLR_PREVIEW_HOME/preview.py" plan-carry-forward \
  --to-sha <FULL_CANDIDATE_SHA> \
  --ids-file <PRIVATE_IDS_JSON> \
  --output <PRIVATE_MANIFEST_JSON>

# 仅用于已单独审查的处置后 Web 后继：
python3 "$DLR_PREVIEW_HOME/preview.py" plan-carry-forward \
  --mode audited-web-same-schema-v1 \
  --to-sha <FULL_CANDIDATE_SHA> \
  --ids-file <PRIVATE_AUDITED_IDS_JSON> \
  --output <PRIVATE_MANIFEST_V3_JSON>

python3 "$DLR_PREVIEW_HOME/preview.py" select <PR_NUMBER> \
  --carry-forward <PRIVATE_MANIFEST_JSON>
python3 "$DLR_PREVIEW_HOME/preview.py" resume
```

`plan-carry-forward` 只接受当前所选 PR 的 eligible HEAD，要求候选镜像已由普通 watcher stage，并使用 `prepare-sandbox-host.sh --status` 只读核对 keeper。计划绑定仓库、PR、旧/新 SHA、旧/新 schema、迁移图、控制器文件、镜像、全部命名卷及明确选择；原 manifest 不能自动重绑到新的 HEAD 或控制器。`select` 将其复制进控制器自己的私有目录，配置和 `status` 只保留 manifest ID、摘要及候选绑定，不显示 Execution 列表、路径、卷名或 journal 内容。普通 `select` 会清除旧引用。

对于 manifest v2，本控制器唯一支持的同 schema 保全升级是 `0040_issue152_dispositions` 到同一 revision，且不得新增或删除任何 schema 对象。新候选 SHA 必须重新生成 manifest，只有现有已支持的责任分类可以进入计划。`execution_incident_dispositions` 审计表必须存在，并在计划及后续每次核验时保持为空；已有任一 disposition 会在停止服务前直接拒绝 v2 规划。既有 `runtime_reconciliation_cursors` 表保留在原 inventory 中，后继不重新执行 0039 seed；当前游标不要求等于初始 `0/0`，应用运行期间正常 reconciler 仍可推进游标。未知的同 revision 或向前 transition 在 manifest 校验时拒绝。这条显式 v2 后继路径不是通用的同 schema 部署机制。

固定预览在处置完成后还有一条例外，但只供已单独审查的 Web 更新使用。必须显式传入 `--mode audited-web-same-schema-v1`；省略该参数仍执行上面的 audit-empty manifest v2 合同。此模式生成 manifest v3，只接受 `0040_issue152_dispositions` → `0040_issue152_dispositions`，并在规划、选择和切换前最后检查时重新计算完整 Git 对象差异。唯一允许变化的产品源码是 `web/src/index.css`；对应测试、控制器、双语文档和 Issue 161 planning 文件使用闭合配套列表。新增、删除、重命名、symlink、submodule、mode 变化、依赖或迁移变化及未知路径均拒绝。

此模式的私有 IDs 文件须增加非空 `terminal_executions`。每项绑定一个原 Execution、Incident、disposition UUID、终态与代次、输出摘要、两个错误码和 Attempt 数量。预期必须来自已单独封存并复核的验收快照，不能直接把 fresh 行回填成自我批准计划。queued 与 terminal 身份不得重叠；terminal 可以同时进入 `cleanup_execution_ids`，仍为 pending/deferred cleanup 的 terminal 必须显式进入。规划在同一个只读事务内读取完整 17 列审计表及真实 `id` 主键，并与原十三张责任表一起验证。全审计表必须精确等于显式 disposition 集；验证器还核对 actor、请求摘要、Incident/Outbox 关系、终态、无 replay、资源释放和 Adapter/global Admission 总量，并在后续各阶段保持完整十四表投影。已 published 的取消 Outbox 原行保持不变，`last_error_code` 可以继续为 null；规范取消码属于 Execution 与 disposition 审计事实。

### 第二组一次性 v4 保全更新

`audited-group2-same-schema-v1` 只用于已批准的第二组精确候选。它不扩大普通模式、manifest v2 或第一组 v3 的适用范围，也不能用于新的提交。最终提交完成独立审查和精确 CI 后，integration owner 制作 `group2-reviewed-scope-v1` 私有记录；记录绑定完整 64 路径 raw diff、54 个冻结产品 blob、六个运行时 controller 文件、迁移图、四项 CI、镜像、批准副本、审查报告和第一组保全参考。

review scope JSON 旁必须有同名 `.evidence` 目录。例如 `review-scope.json` 对应 `review-scope.evidence/`；目录内只有以下固定文件，文件名中的摘要是文件本身的 SHA-256：

```text
approval/REQUEST-ready.md
approval/USER-APPROVAL.json
approval/product-scope.json
approval/review-bindings.json
reviews/<REPORT_SHA256>
ci/<EVIDENCE_SHA256>
preservation/<REVIEW_REPORT_SHA256>
```

CI 原件必须是 GitHub REST 的完整 `{run,jobs}` 聚合：`run` 保留 Actions run 原对象，`jobs` 保留带 `total_count` 的完整 jobs 响应。控制器核对 run 的 ID、attempt、HEAD、workflow path、event 和成功终态，将原始 jobs 按 `(name,id)` 规范排序后，要求四个 scope job 与原件中的唯一成功 job 完全一致；每个 job 自带的 run ID、attempt 和 HEAD 也必须与 run 相同。独立代码审查报告只能包含一条 `group2-independent-review-v1` machine record，固定字段为 `schema/status/reviewed_commit/source_kind/coverage/blocking_findings`；每个 coverage 项逐 byte 核 Git mode、blob OID 和 SHA-256，重复路径或同报告中的相反结论都会拒绝。保全报告同样只能包含一条 `group2-preservation-review-v1` 记录，并精确绑定 snapshot digest 与 lineage。所有 evidence 目录必须为私有目录，文件必须为私有、单链接 regular file。

先由旧 trusted controller 对精确最终提交完成 stage，并保持原 busy 现场；暂停后才冻结含镜像身份的 scope。随后从该提交的干净工作区调用官方安装器入口。专用入口不接受 `--start`，只在官方安装器真实取得 operation→config 双锁后放行复制，并在安装后保持 paused：

```sh
DLR_PREVIEW_HOME=<PRIVATE_CONTROLLER_ROOT> \
  python3 tools/local-preview/preview.py install-group2 \
  --review-scope <PRIVATE_REVIEW_SCOPE_JSON>

python3 "$DLR_PREVIEW_HOME/preview.py" plan-carry-forward \
  --mode audited-group2-same-schema-v1 \
  --review-scope <PRIVATE_REVIEW_SCOPE_JSON> \
  --to-sha <EXACT_FINAL_SHA> \
  --ids-file <PRIVATE_GROUP2_IDS_JSON> \
  --output <PRIVATE_MANIFEST_V4_JSON>
python3 "$DLR_PREVIEW_HOME/preview.py" select <PR_NUMBER> \
  --carry-forward <PRIVATE_MANIFEST_V4_JSON>
python3 "$DLR_PREVIEW_HOME/preview.py" resume
```

v4 在停 Control 后重新读取完整责任、审计、业务资产、session、文件和日志前缀；再停 Worker/Web/account-web，验证真实 idle kernel，完成 custom-format 备份和同 schema Alembic no-op。从 plan 的原始日志前缀开始，每个停写、备份、迁移、启动、入口检查、probe 和后置健康阶段都连续验证同一文件身份与前缀；阶段转换从上一 append 的完整 end hash 派生下一 baseline，允许正常追加但拒绝截断、替换和旧前缀改写。每个日志端点同时绑定实际时钟证据：Linux 使用同次采样的 `CLOCK_REALTIME_COARSE` 下界和精确观察上界；只有预先列明的新直接普通日志文件才可在该界内改变根目录 mtime，无新增文件时目录与嵌套目录仍须精确不变。该规则处理 Linux 文件时间粒度，不放宽 startup 的精确窗口，也不接受固定容差。入口检查产生的拒绝日志先单独闭合，然后才取得 probe baseline，因此不计入正式 probe；probe 的 partial 只能继续生成 final，控制器再把这两个连续片段合成为唯一完整窗口，不能从同一旧 baseline 分叉。候选同时启动 `control worker web account-web`，account-web 必须使用候选 Web 镜像和原 loopback 绑定。控制器只运行一次官方 RabbitMQ→Worker probe，等待其 cleanup 自然完成，并以本轮日志窗口证明唯一 Adapter/Execution/Attempt/Worker/cleanup 归属。动态启动文件变化、probe 后数据库与文件保全、双入口边界和后置健康全部通过后，才依次写 receipt、current SHA 和 ready，并一次性消费 manifest。receipt 的十个阶段摘要由 VM 原件重算，同时保存完整原始 evidence 对象的摘要；宿主从各阶段 DB、文件、日志、startup/probe proof、cleanup、账号和入口结果重新组成原件，调用同一比较器重算成功后再核这个摘要，并精确绑定 manifest、account profile、startup 后与最终健康检查中的同一 Worker 生命周期、probe、post-preservation、current SHA、ready transaction 与宿主 safe state。中途失败保留 transaction、attention、备份和现场，不恢复旧应用、不重跑 probe、不覆盖已消费 manifest。

同 SHA 离线恢复只接受最后成功 state、ready transaction、私有 receipt 和已消费 v4 manifest 全部一致的提交。任何 Compose 启动或 recreate 前先持久写入宿主 attention 和 VM `recovering` transaction，并为本轮生成唯一 recovery ID；数据库或 Broker 启动、schema 检查及其后任一门禁失败都不能留下旧 ready。部署 Ready 前会把最终 DB/文件保全基线、后置健康日志端点和选择集保存为 receipt 原始证据绑定的不可变恢复根；第一次恢复的 fresh before 必须与该根严格一致，后续恢复只能使用宿主 safe state 与 ready transaction 共同绑定、且完整重算通过的前一次 completion.after 作为 predecessor，fresh before 必须与 predecessor 严格一致。恢复不会重新 capture 日志来建立自证基线，而是从部署末端或前一次已验证末端继续 append，再隔离本轮 startup 窗口。宿主与 VM 都沿 completion 的 predecessor 逐节点重放到原 receipt 根，拒绝缺失、循环、错误 ID/摘要、失败结果或被替换的祖先。每轮只以新 container、StartedAt、唯一 startup preflight、连续日志窗口和动态文件比较证明本轮允许的两个 mtime 推进；未知业务新增、旧行/审计/资产/session/文件变化、分叉、回退、循环或损坏 predecessor 都 fail closed，不会把 fresh capture 自批为新根。随后恢复其余三个应用并复核账号绑定、真实 account CSRF GET 与完整双入口边界。恢复的最终 health、cgroup 和五服务镜像检查全部成功后，才写入绑定 recovery ID、manifest、predecessor 和本轮原始证据摘要的 append-only completion，最后把同一 ID/摘要写入 ready transaction 和宿主 safe state；其中任何一步中断都保持 `recovering`。宿主成功路径与 `acknowledge` 都重新读取本轮 completion、全部祖先和原始证据并复算，不能借用旧部署证据清除 attention。恢复不重新检查 GitHub/CI，不执行迁移、正式 probe、业务清理或第二次消费。receipt/profile/恢复基线缺失、account-web 漂移或保全摘要不一致均进入 attention；`acknowledge` 不能绕过这些检查。`status` 只显示安全摘要，不显示账号端口、日志、session、mount 或私有对象 ID。

验证器在 REPEATABLE READ READ ONLY 事务中按固定 allowlist 读取 Execution、Attempt、Slot、Incident、Outbox、Adapter/Global Admission、Input Lease/Hold、Credential Snapshot、idempotency、schedule outcome 和 Worker cleanup request。它保存旧列、主键、逐行哈希和计数，不把原数据库值写到公开回执。只有清单内 queued＋open Incident、未释放 Admission、当前代 Outbox、无 active Attempt/Slot，且没有其他 queued/running/retry_wait 或 Worker cleanup 责任时才通过。

cleanup 只从事实派生，不写数据库：

- `not_applicable`：零 Attempt、`attempt_count=0`、无 worker/start、无 workspace/journal/Sandbox 证据；原 `pending` 保持不变。
- `completed`：已有终态 Attempt、数据库已为 `completed`，且无残留 workspace/journal。
- `deferred_preserved`：任一历史终态 Attempt 的 cleanup 摘要仍为 `deferred`，其私有 cleanup journal 的 Execution、Attempt、路径和 Token 摘要与数据库一致；即使后继 Attempt 已使 Execution cleanup 显示 completed，这份旧责任仍须由真实 Worker receipt 收敛。

journal 缺失、未知文件或 symlink、未选择的 workspace、未知 cgroup、活动 Slot/Attempt、额外 open Incident、材料树漂移都阻断升级。验证只读挂载 Worker runtime/journal 和 Control 的 builtin/artifact 卷；依赖缓存不作为 Execution 责任，但命名卷身份仍固定并保留。

切换时先复核 manifest，再停 Control 并重读数据库；通过后才停 Worker/Web，确认应用容器已停止、keeper 身份未变且委派树只剩 `agent`。停写后、备份后、迁移后新服务启动前，旧数据库列投影、责任分类、journal/runtime/材料树和 kernel 证据必须一致。迁移允许增加本版本的新列/表，但比较仍使用 manifest 记录的全部旧列。原 `assets.py`、备份可列出、镜像、CI/历史、Sandbox、真实 RabbitMQ→Worker 执行和 workspace cleanup 门禁继续执行。

carry-forward 切换停下 Control 后若出现 Claim、证据变化或任何未知读取失败，控制器保持应用停止和 attention，要求人工核对并重新计划；它不会用一次旧健康结果自动恢复写入。进入 `migrating` 后同样不自动 downgrade、restore 或启动旧 schema 应用。失败现场、原卷和备份保留供诊断。成功 receipt 只记录 manifest ID/摘要/计数，不公开私有选择；它证明旧责任被原样带到新版本，不证明原 Incident 已恢复、终结或 cleanup 已完成。后续验收必须关联原 Execution ID、generation、Attempt、输出与资源释放，新建任务成功不能替代。

### 第二组 starting 事故的受限软件恢复

本入口只处理本组已绑定的首次启动后、正式 probe 前失败；它不是普通 `recover`，也不把失败部署补记成成功。**代码、测试与 CI 通过不等于事故操作获批**。实际传输工具、恢复旧软件和提交控制面对账前，必须取得绑定具体请求摘要、工具 SHA 与实际用户原文的专项批准；原第二组通用交付批准不能替代。

从独立审查且精确 CI 通过的干净仓库运行官方入口：

```sh
DLR_PREVIEW_HOME=<PRIVATE_CONTROLLER_ROOT> \
  python3 tools/local-preview/preview.py reconcile-group2-starting \
  --incident-request <PRIVATE_INCIDENT_REQUEST_JSON> \
  --incident-approval <PRIVATE_INCIDENT_APPROVAL_JSON>
```

闭合请求绑定失败 manifest 与首次 startup 原件、最后成功 receipt/consumed/镜像、完整源码范围、审查/CI、原卷与账号绑定；同名 `.evidence` 目录只接受固定清单中的私有单链接普通文件。批准动作必须精确覆盖事故工具暂存、旧软件恢复和控制面对账。没有任意命令、目标 SHA、force、resume 或 retry 选项；同事故 ID 已存在即拒绝重放。

正式安装器对 attention 的拒绝保持不变。事故入口取得宿主 operation→config 锁及 VM deploy 锁，先核实际批准/源码/CI、host/VM authority、正式安装字节、镜像/卷/PG 身份，再把同提交的 deploy/carry 工具暂存到私有事故目录并验证摘要。工具就位后完成完整 fresh 数据、文件、日志和启动核验，全部通过前不改 transaction phase 或停服务；失败只留下 prepared 目录，原告警与运行状态不变。正式宿主/VM 控制器、installation 和旧成功记录不替换；不能临时移走 attention 或让读取返回伪造状态以通过安装器。

恢复先停 Control 并复核，随后停 Worker、Web 和 account-web，验证原 DB、完整责任/审计、全部业务资产与 session、文件、连续日志及真实 idle kernel/namespace/FD。旧 PostgreSQL 镜像必须实际存在，版本、完整 RootFS 和数据卷符合绑定；仅重建软件容器，不恢复备份、不执行迁移、不写旧业务行。之后启动旧成功版本的 Control、Worker、Web。RabbitMQ 与全部卷保持，account-web 保留当前容器、镜像和绑定并停止，不要求把它伪报为健康。

第二次 startup 使用独立精确窗口、唯一 Worker/nonce、连续日志及既有两处允许的目录 mtime 证明；其余旧内容、权限和 DB 不变，Token 入口只做只读健康检查，不运行正式业务 probe。完整事故原件与独立 receipt 先在 VM 持久化，再由宿主读回重算和保存。只有成功后才提交指向旧成功 SHA 的事故恢复 transaction；原 current SHA、宿主 state、旧成功 probe/receipt/consumed 保持原字节。失败 manifest 原样归档并记录 abandoned，绝不 consumed；配置 CAS 清除旧 carry 引用并保持 paused，attention 最后清除。

任何读取、保全或持久化失败都停在真实阶段，不自动重试、回退数据库或清理原件。receipt 后、清 attention 前中断也不自动续作：先只读对账，再审查具体剩余动作。恢复成功只证明旧软件已恢复和现场被保留，不证明新候选部署成功，也不证明账号入口可用。

恢复后的新保全报告保留现有 snapshot/reference shape，仅为本事故使用 `group2_starting_reconcile_v1` 来源及唯一 `group2-reconcile-chain-v1` 记录。验证器从原参考重算两个 startup 与完整保全链，唯一导出原 selection/DB、恢复后 files 及追加 request/receipt/chain 摘要的 lineage；fresh 值不能自行批准。独立 reviewer 签署后，才重新执行 trusted stage、新 scope、官方 install、fresh manifest 与正常 v4 部署；最终四应用、双入口及正式 probe 的原门禁全部保留。

## 安装或更新控制器

当前安装器用于接管私有配置指定的既有环境，要求 macOS、Python 3.11+、`gh` 登录、Colima、已有 LaunchAgent、`source.git` 源码缓存、`config.json`、`state.json`、`preview.env`，以及 VM 内已准备好的 sandbox 脚本。它不负责首次创建 VM 或生成凭据，也不改变默认 Docker context。若 carry-forward plan 正在占用配置，安装器会先等待它结束再暂停更新；暂停后若 controller operation 仍忙，安装器保持 paused 并退出。取得操作与配置边界后，它才卸载 watcher；随后必须取得 watcher singleton，才会备份、替换文件或传输 VM 脚本。

从审查过的仓库工作区运行：

```sh
python3 tools/local-preview/install.py --start
```

安装前把旧控制器、配置、凭据与状态复制到权限受限的 `controller-backups/<timestamp>/`，暂停跟踪并卸载旧 LaunchAgent，然后替换脚本。正在构建或切换时会先暂停后续更新，要求等当前动作完成再安装。首次接管以当前容器 image ID 为旧 SHA 建立镜像记录，保留全部配置与数据。安装记录包含来源路径及脚本 SHA-256。

安装前设置 `DLR_PREVIEW_HOME`，并在该目录的私有 `config.json` 中提供：

| 字段 | 用途 |
| --- | --- |
| `repo`, `pr`, `enabled`, `poll_seconds` | 仓库、目标 PR、开关与轮询间隔 |
| `profile`, `project` | 已有 Colima profile 与 Compose project |
| `vm_root`, `web_port` | 已有 VM 安装目录与本地应用端口 |
| `launchagent_label` | 已有 LaunchAgent 的标识 |
| `sandbox_unit`, `sandbox_cpu_quota`, `sandbox_memory_max` | 已有 Sandbox 父级单元与资源上限 |

这些值没有个人环境默认值；安装器校验配置并向 VM 写入权限受限的部署参数文件。LaunchAgent 通过环境变量读取私有安装目录。配置、凭据、备份和原始运行回执均不得提交到公开仓库或 PR。

VM 根目录下可选的 `build.env` 保存本机既有构建代理环境（HTTP_PROXY/HTTPS_PROXY）；代理地址不是项目默认值。`preview.env`、Docker 代理及 Git 缓存配置始终留在本机，禁止提交到仓库。

## 离线恢复与故障处理

后台发现 VM 或应用停止且不存在未完成部署时，使用最后成功 SHA 的精确镜像恢复；不重新构建，不迁移数据库，不依赖 GitHub。恢复前后核对 transaction、数据库 revision、镜像 ID。仅应用健康异常且容器仍运行时保留现有进程，记录需要处理，避免中断正在测试的工作。

正常释放运行内存并保留数据：

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" pause
colima stop "$PREVIEW_PROFILE" # 使用私有配置中的 profile
# 之后恢复跟踪，后台会先恢复原环境。
python3 "$DLR_PREVIEW_HOME/preview.py" resume
```

构建失败时旧应用不受影响。切换开始前持久保存 attention，任何中断或迁移/验证失败都会保留此记录，禁止下一轮盲目重试或把旧健康结果当成新部署成功。

发生 attention 时先读 `status`、本机 `deploy.log` 与 VM `transaction.json`，确认实际数据库 revision、current-sha、候选 receipt 和备份。根据实际失败修复并验证；数据库还原须明确选择备份并停止写入，不执行自动 downgrade。只有 VM transaction 已恢复为最后成功部署的 ready 状态，才可用下列命令清除 attention：

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" acknowledge
```

若候选已经完成但宿主在提交状态前退出，应先核对该 SHA 的 receipt、镜像及数据库，按回执人工恢复 `state.json`，再 acknowledge。不要仅删除 attention 来跳过检查。

## 开发验证

```sh
python3 -m unittest discover -s tools/local-preview/tests -v
python3 -m py_compile tools/local-preview/*.py tools/local-preview/tests/*.py
bash -n tools/local-preview/deploy.sh
openspec validate issue161-runtime-reliability --type change --strict --no-interactive
git diff --check
```

控制器测试独立于业务后端，由 CI 的 `local-preview` job 执行；数据库迁移和真实执行仍由业务 CI 与实际预览验证提供证据。
