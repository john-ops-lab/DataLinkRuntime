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

python3 "$DLR_PREVIEW_HOME/preview.py" select <PR_NUMBER> \
  --carry-forward <PRIVATE_MANIFEST_JSON>
python3 "$DLR_PREVIEW_HOME/preview.py" resume
```

`plan-carry-forward` 只接受当前所选 PR 的 eligible HEAD，要求候选镜像已由普通 watcher stage，并使用 `prepare-sandbox-host.sh --status` 只读核对 keeper。计划绑定仓库、PR、旧/新 SHA、旧/新 schema、迁移图、控制器文件、镜像、全部命名卷及明确选择；原 manifest 不能自动重绑到新的 HEAD 或控制器。`select` 将其复制进控制器自己的私有目录，配置和 `status` 只保留 manifest ID、摘要及候选绑定，不显示 Execution 列表、路径、卷名或 journal 内容。普通 `select` 会清除旧引用。

本控制器唯一支持的同 schema 保全升级是 `0040_issue152_dispositions` 到同一 revision，且不得新增或删除任何 schema 对象。新候选 SHA 必须重新生成 manifest，只有现有已支持的责任分类可以进入计划。`execution_incident_dispositions` 审计表必须存在，并在计划及后续每次核验时保持为空；已有任一 disposition 会在停止服务前直接拒绝规划。既有 `runtime_reconciliation_cursors` 表保留在原 inventory 中，后继不重新执行 0039 seed；当前游标不要求等于初始 `0/0`，应用运行期间正常 reconciler 仍可推进游标。未知的同 revision 或向前 transition 在 manifest 校验时拒绝。这条显式后继路径不是通用的同 schema 部署机制。

验证器在 REPEATABLE READ READ ONLY 事务中按固定 allowlist 读取 Execution、Attempt、Slot、Incident、Outbox、Adapter/Global Admission、Input Lease/Hold、Credential Snapshot、idempotency、schedule outcome 和 Worker cleanup request。它保存旧列、主键、逐行哈希和计数，不把原数据库值写到公开回执。只有清单内 queued＋open Incident、未释放 Admission、当前代 Outbox、无 active Attempt/Slot，且没有其他 queued/running/retry_wait 或 Worker cleanup 责任时才通过。

cleanup 只从事实派生，不写数据库：

- `not_applicable`：零 Attempt、`attempt_count=0`、无 worker/start、无 workspace/journal/Sandbox 证据；原 `pending` 保持不变。
- `completed`：已有终态 Attempt、数据库已为 `completed`，且无残留 workspace/journal。
- `deferred_preserved`：任一历史终态 Attempt 的 cleanup 摘要仍为 `deferred`，其私有 cleanup journal 的 Execution、Attempt、路径和 Token 摘要与数据库一致；即使后继 Attempt 已使 Execution cleanup 显示 completed，这份旧责任仍须由真实 Worker receipt 收敛。

journal 缺失、未知文件或 symlink、未选择的 workspace、未知 cgroup、活动 Slot/Attempt、额外 open Incident、材料树漂移都阻断升级。验证只读挂载 Worker runtime/journal 和 Control 的 builtin/artifact 卷；依赖缓存不作为 Execution 责任，但命名卷身份仍固定并保留。

切换时先复核 manifest，再停 Control 并重读数据库；通过后才停 Worker/Web，确认应用容器已停止、keeper 身份未变且委派树只剩 `agent`。停写后、备份后、迁移后新服务启动前，旧数据库列投影、责任分类、journal/runtime/材料树和 kernel 证据必须一致。迁移允许增加本版本的新列/表，但比较仍使用 manifest 记录的全部旧列。原 `assets.py`、备份可列出、镜像、CI/历史、Sandbox、真实 RabbitMQ→Worker 执行和 workspace cleanup 门禁继续执行。

carry-forward 切换停下 Control 后若出现 Claim、证据变化或任何未知读取失败，控制器保持应用停止和 attention，要求人工核对并重新计划；它不会用一次旧健康结果自动恢复写入。进入 `migrating` 后同样不自动 downgrade、restore 或启动旧 schema 应用。失败现场、原卷和备份保留供诊断。成功 receipt 只记录 manifest ID/摘要/计数，不公开私有选择；它证明旧责任被原样带到新版本，不证明原 Incident 已恢复、终结或 cleanup 已完成。后续验收必须关联原 Execution ID、generation、Attempt、输出与资源释放，新建任务成功不能替代。

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
