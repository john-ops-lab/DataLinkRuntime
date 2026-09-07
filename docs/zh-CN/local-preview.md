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

## 升级规则

候选必须包含已部署 Git 提交；当前数据库 revision 必须位于唯一、完整、未改变既有 revision/down_revision 的 Alembic 迁移链中。历史已应用迁移的函数修复允许存在，实际升级只执行当前数据库版本之后的迁移。历史分叉或无法向前迁移时记录原因，保留现场，不自动降级或另建环境。

构建发生在无宿主目录共享、无 SSH Agent 转发的 VM 内，源码使用完整 SHA 提取。宿主 GitHub 登录凭据不传给 VM。镜像采用完整 SHA 标签并记录 image ID；构建完成后再次检查目标、HEAD、最新 CI 和数据库版本，才进入切换。

切换先检查执行与清理空闲，停止 Control 的 API、调度器和重试写入，再检查一次；如有竞争中的执行，恢复旧 Control 并等待。空闲后停 Worker/Web，保存并校验 PostgreSQL custom-format 备份，再迁移数据库。升级前后比较既有适配器、版本、权限、配置、模板、凭据、材料元数据等行的哈希，允许迁移新增默认数据，但不得改写或丢失旧资产。恢复应用后验证精确镜像、私有 cgroup、健康，以及一次真实 RabbitMQ → Worker 执行和工作区清理。全部通过才更新 deployed。

数据库备份保存在 配置的 VM 根目录下 `backups/<timestamp>-<old>-to-<new>/`。资产清单仅含列名与行哈希，备份文件本身包含业务数据，目录权限受限。数据库备份不包含材料卷；部署从不删除任何卷。

## 安装或更新控制器

当前安装器用于接管私有配置指定的既有环境，要求 macOS、Python 3.11+、`gh` 登录、Colima、已有 LaunchAgent、`source.git` 源码缓存、`config.json`、`state.json`、`preview.env`，以及 VM 内已准备好的 sandbox 脚本。它不负责首次创建 VM 或生成凭据，也不改变默认 Docker context。

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
bash -n tools/local-preview/deploy.sh
openspec validate local-preview-delivery --strict
```

控制器测试独立于业务后端，由 CI 的 `local-preview` job 执行；数据库迁移和真实执行仍由业务 CI 与实际预览验证提供证据。
