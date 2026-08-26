## Purpose

定义从旧 manual/Schedule 输入和 Worker v1 平滑迁移到统一输入与文件协议的发布、特性开放和回滚门禁，避免滚动部署中的双权威与数据丢失。

## ADDED Requirements

### Requirement: 基线数据迁移确定且可审计
迁移 SHALL 把已有 Schedule JSON 回填为对应 `AdapterInputConfig(source_type=json)`，把没有持久输入的已有 manual Task Adapter 回填为 `json/{}`，新建 Task Adapter 默认 `none`，回填 revision 为 1；历史 Execution input MUST 保持原样。

#### Scenario: 从固定基线升级
- **WHEN** 数据库从 Issue 固定基线升级到新 schema
- **THEN** 迁移输出 Adapter 总数、各 source type 数量与冲突数量，且 Scheduler 后续以 InputConfig 为新权威

#### Scenario: 重复执行回填
- **WHEN** 迁移/校验脚本对已正确回填数据再次执行
- **THEN** 结果幂等、计数一致，不递增 revision或创建重复配置

#### Scenario: 冲突或孤儿数据
- **WHEN** 既有 InputConfig 与计算结果不同、Schedule 重复/孤儿或所有权不一致
- **THEN** 迁移失败并列出 Adapter ID 与稳定原因，不静默选择一方

### Requirement: 旧 manual 输入兼容窗口有期限
`legacy_input_compat_enabled` 开启时旧 per-run input SHALL 仅作为本次 Execution 输入，不改写 AdapterInputConfig，并记录弃用指标/脱敏日志；关闭后任何出现 `input` 字段的请求 MUST 返回 `execution_input_override_not_supported`。

#### Scenario: 兼容开关开启的旧 Web
- **WHEN** 旧 Web 发送 per-run JSON input
- **THEN** Control 按旧语义创建当次 Execution、记录兼容指标，Adapter 当前输入与 revision 不变

#### Scenario: 关闭前观测
- **WHEN** 旧式 manual 调用连续 48 小时未归零
- **THEN** 发布 Gate 不允许关闭兼容开关

### Requirement: 旧 Schedule input 在观察窗口双向镜像
兼容开关开启时，旧 Schedule PUT input 与新 JSON InputConfig SHALL 双向镜像并在同一受控写入中保持一致；Scheduler MUST 只使用统一 resolver，旧列仅作为回滚镜像。观察窗口结束后才可由独立后续迁移删除旧列。

#### Scenario: 旧客户端更新 Schedule
- **WHEN** 旧客户端在兼容期修改 Schedule input
- **THEN** 新 InputConfig 与旧列原子镜像为同一值，冲突失败不得留下分叉

#### Scenario: 新客户端更新 JSON 输入
- **WHEN** 新 Web 保存 json InputConfig
- **THEN** 兼容期内旧 Schedule input 同步更新，已创建 Execution 不受影响

#### Scenario: 兼容关闭后的旧字段
- **WHEN** 旧 Schedule 请求仍携带 input
- **THEN** Control 明确拒绝而不是忽略，且不修改 Schedule cursor或 InputConfig

### Requirement: Worker v1/v2 滚动升级独立于文件 feature flag
Control SHALL 先支持 v1/v2 与 nullable Token hash，并以 `DLR_MIN_WORKER_PROTOCOL_VERSION=1` 部署；v1 只领取 none/json，v2 才获得 Token。只有目标 Worker 全部 v2且 v1 active Execution 排空后才能开放 managed_files。

#### Scenario: 混合 Worker 池
- **WHEN** v1/v2 Worker 共存且 managed_files flag 关闭
- **THEN** none/json 可按最低协议运行，v1 Result 继续按旧合同上报，不会领取其无法完成的文件任务

#### Scenario: 提高最低协议
- **WHEN** 连续 48 小时没有 v1 claim且所有 v1 active Execution 已终态
- **THEN** 可把最低版本提升到 2，之后旧 Worker claim 返回 `worker_protocol_incompatible`

### Requirement: Managed Files feature flag 按完整能力门禁开放
`DLR_MANAGED_FILES_ENABLED` 默认关闭；Wave B 存储链路和 Wave C Worker/恢复链路的自动 Gate、故障注入和协议排空条件全部通过前 MUST 不向用户开放选择、上传或文件 Execution。

#### Scenario: 仅后端上传完成
- **WHEN** ArtifactStore/上传已实现但 Worker v2 cleanup recovery 未通过
- **THEN** flag 保持关闭，Web 文件卡片禁用，Control 拒绝 managed_files 保存/运行的公开路径

#### Scenario: 开放前检查
- **WHEN** 准备打开 flag
- **THEN** 发布证据证明目标 Worker 均 v2、无 v1 active Execution、Lease/下载/清理/故障 Gate 通过

### Requirement: Wave A-D 顺序交付且不得拆成多 PR
本 Issue SHALL 在同一功能分支按 Wave A 统一 none/JSON、Wave B 设置/Artifact/GC、Wave C Lease/Worker/Runtime/recovery、Wave D UI/history/copy/full acceptance 顺序实施；任何 Wave Gate 失败 MUST 阻止进入下一 Wave，最终远端交付只形成一个完整 PR。

#### Scenario: Wave B 未通过 GC 故障 Gate
- **WHEN** GC 幂等、reservation 并发或 Adapter 删除测试失败
- **THEN** 不进入 Wave C，不打开文件 feature flag，也不提交最终 PR

#### Scenario: 本地批次并行
- **WHEN** 同一 Wave 内无公共 schema/API/protocol/迁移依赖且文件与资源隔离
- **THEN** 可在独立 worktree/数据库/Compose project/端口中并行验证，但本地 main 集成始终逐批串行

### Requirement: Compose 明确持久卷与单 Control 边界
部署 SHALL 为 Control ArtifactStore 和 Worker Cleanup Journal 提供独立持久挂载，并配置全部有界环境变量；LocalFileArtifactStore 第一阶段 MUST 只支持单 Control writer，多 Control 需要后续共享一致性 ArtifactStore。

#### Scenario: Fresh Compose
- **WHEN** 使用隔离 Compose 从空卷启动并应用 Alembic head
- **THEN** Control/Worker 校验目录权限与超时不变量，上传/重启后 Blob 与 cleanup journal 持久，三语言执行成功

#### Scenario: 多 Control 配置
- **WHEN** LocalFileArtifactStore 检测到多 writer 部署
- **THEN** 启动或健康 Gate 明确失败并指向单 Control 限制，不宣称支持共享写入

### Requirement: 文件能力回滚非破坏且有观察窗口
回滚 SHALL 先关闭 feature flag、禁止新上传和新 managed_files Execution，等待 active Execution 终态；保留 Artifact/Reservation/Binding/Lease/Deletion Job 表和 Blob 供恢复/治理，不执行破坏性 schema downgrade。

#### Scenario: 回滚期间已有文件 Execution
- **WHEN** 决定回滚但存在 pending/running managed_files Execution
- **THEN** 系统停止新增、等待其终态与 Lease 释放后再回滚 Worker/Control，不删除其 Blob

#### Scenario: 旧列移除门禁
- **WHEN** 新版运行未满 7 天或某个 enabled Schedule 未覆盖至少两个真实计划点
- **THEN** 不停止旧 Schedule input 镜像、不删除旧列

### Requirement: 发布验证区分自动 Gate 与人工验收
最终候选 SHALL 对精确 SHA 记录 migration、单元/集成、并发、故障注入、三语言、Compose、Web 双语视口、回滚演练结果；自动结果 MUST 与 retained-app 用户最终视觉/业务验收分开报告。

#### Scenario: 自动 Gate 通过
- **WHEN** 所有机器 Gate 对 Candidate SHA 通过
- **THEN** 结果只证明候选可进入人工验收，不自动标记用户 PASS

#### Scenario: 人工验收失败
- **WHEN** 用户在 retained app 判定视觉或业务 FAIL
- **THEN** 候选不得视为最终完成，后续整改必须产生新 SHA并重新运行受影响 Gate
