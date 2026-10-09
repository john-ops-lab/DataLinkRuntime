## Context

见 proposal.md。固定基线已用实际 uv 安装链复现 #169：包安装成功、`.venv/.lock` 为 `0666`、缓存提交抛 `cache_permissions_invalid`。当前只在 builtin_materials 分支修正该锁文件。#178 的模板使用 `S3Client.Builder`，SDK 提供的是 `S3ClientBuilder`。#173–#176 的审查线索仍需真实可达回归和真实 Broker 责任证据。

## Goals / Non-Goals

**Goals:** 每个 Issue 有独立原因/回归/最小提交；证据区分宿主直接链、模拟测试和真实 Worker/UI。阶段诊断只记录稳定分类及计量。

**Non-Goals:** 不修改 ACK 时点、业务终态、原始校验预期或生产策略，不添加任意 Prompt/响应存储、新 Agent 框架、HA、无限自动重试和历史会话。

## Decisions

1. **固定基线和隔离工作区。** 在 `codex/issue168-reliability` 上开发，原 main 及未跟踪验收资料保持。每个修复先记录基线结果；共享代码按明确顺序整合，不以 Issue 数量强制拆分 PR。
2. **#169 修正特定安装器元数据。** 将现有 uv 锁文件的私有权限处理覆盖普通源和内置源，放在安装完成、缓存提交之前。其他缓存权限和摘要校验不变。拒绝修改 umask 或跳过缓存校验，因为 uv 会显式创建可写锁而包内容仍须严格校验。
3. **#178 按真实声明 SDK 编译。** 使用 SDK 公共 builder API；发布资源哈希与出处记录同步，以实际编译和合成目标内容核对代替字符串测试。
4. **#173–#176 先证实触发与责任。** 删除残留与坏记录隔离分开归因；queued guard 冲突必须来自支持路径；Worker 可重试等待与连接故障分类按实际 Broker 投递与恢复测试决定最小改法。数据库/I/O 故障不纳入可吞局部错误。已用公开 Claim/Start/guard acquire 建立 running replacement owner 与同键 queued 的支持前态；真实 RabbitMQ 4.3.5、默认 delivery_limit=5 下，12 秒观察到 7 个连接 epoch、两条消息投递计数 0→5 且 DLQ=2（Worker 资源 envelope 为明确测试替身，尚不替代 Linux 执行业务验收）。#175 只捕获 cache_reclamation_in_progress，持久化 manual_review Incident 后 ACK，其他错误继续失败。#176 在相同锁顺序内校验完整 dispatch、执行取消，再将同代 Outbox 原行转 pending；commit 后返回 ACK_NOOP/cache_dispatch_deferred。Relay 对这类已持久延后且 guard 非 idle 的键暂不 lease，解除后复用原消息身份。保持 EXECUTE 的 Claim/journal 后 ACK；不增代、不造 Attempt、不释放 admission、不调高投递上限。PAUSE/DEFER 不再重置连接故障退避。#177 的非法消息固定 invalid，在现有 last_error 仅记录 allowlist broker_reason，不记录正文。
5. **#180/#183 非敏感诊断。** 公共错误码保持兼容；内部分类使用固定阶段/原因枚举，禁止拼接 exception repr、字段值或原始消息。确定性坏响应用于验证分类和脱敏，真实 Provider 结果另行记录。固定内部类别覆盖 provider_json、provider_envelope、final_json、output_schema、unicode、output_safety、candidate_configuration；不向公共错误增加字段。预算/结果/失败事件复用现有有界 AI audit，以请求 ID 和逐次检查序号关联，输出估算方法、分项、裁剪、相对时限和固定结果类别；拒绝调用也记录预算事实。#182 仅以对象身份定位已经找到的当前 envelope，保留完整工具配对。
6. **#179/#195 的已确认决定（2026-10-09）。** #179 按 Unicode 码点比较，区分大小写、稳定排序，不受 locale 影响；null/boolean 转字符串采用 `null`、`true`、`false`。保持既有跨语言同结果要求，固定共享 fixtures 后修复，不能为各实现建立不同 oracle。#195 提供“停止等待”：立即中止本页 HTTP 等待、隔离本轮迟到消息/Candidate、允许再次发送；既有可见历史保留，不自动 Apply/Save/Run，也不保证撤销 Provider 调用或计费。上游继续受现有共享 deadline/工具预算约束，不增加后台任务、取消服务或持久会话。用户已选择以上推荐方案，并要求后续相似决定按推荐方案自主推进，记录依据。

7. **#173/#184 实际运行根因与审计。** 无依赖真实 Linux Python 任务在取消和 workspace cleanup 完成、Adapter 删除后，环境清理仍失败 3 次，最早阻止点为 cache_rebuild_unknown。可重建证明服务于 live-version GC；deleted-Adapter cleanup 以 Control 验证当前 claim 和无未完成引用的精确 guard 为授权，所有身份、内容、Pin、最近使用、use/journal 校验不变，普通 GC 仍要求证明。扫描完成后等待已有删除恢复，避免重复发起；Worker 报告只增加固定内部原因。#184 复用现有 Worker 轮转日志，round ID 关联 scan/result、模式、预算、游标摘要和最多 200 安全数字键，剔除 identity/proof/路径/任意目录名。

## Risks / Trade-offs

- 宿主 uv 直接链不能证明 Linux sandbox/cgroup/完整 Claim 链 → 直接回归和真实 Worker/UI 验收分别列账。
- 一份代码审查线索未必是现场首个失败根因 → 先建立支持路径和基线；未复现保留条件，不借机扩大修改。
- 异常隔离可能掩盖真实磁盘/数据库故障 → 固定可识别的记录级错误范围，基础设施失败仍可见。
- 取消本页请求不等于 Provider 撤销 → 在文案、决定和验收中明确二者，未决不实现。

## Migration Plan

优先采用无需迁移的小修复和非敏感日志计量；每批提交执行受影响静态/模拟检查，并在私有固定验收配置具备条件时核实际镜像/SHA。专用无宿主挂载 Docker VM 已启动；独立 PostgreSQL/RabbitMQ/只读 S3 测试目标已经建立，Linux Worker/UI 完整验收仍需推进。保持旧数据、缓存、卷与 Token；若某修复需要改动持久合同，先补充设计和兼容/回滚证据。Issue 仍按其完整验收条件判断，不能因一个提交或 CI 通过自动关闭。

## Frontend implementation boundaries

#185 共享页面始终说明 EDIT 用户可通过代码和运行使用已绑定凭据，Secret 不回显，ACL 不变。#186 按后端相同的 trim 后 Unicode 字符数量校验 128 上限，不截断输入。#187 使用保留原始文本的整数秒输入并按服务端 capability 校验上下限，失焦/Enter 不改值；非法值在字段内显示并阻止保存。#188 浏览器 focus/visibility 与打开选择器只刷新凭据元数据；每个请求带本地递增序号，旧结果不覆盖新列表，删除后的选中引用留在草稿并给可修正的失效提示；后端不存在/权限保护保持。#189 目录请求错误独立于变更错误，由最新成功目录请求清除。#190 共享小 hook 在 overlay 自动聚焦前捕获原控件，关闭动画或条件卸载后恢复到仍可见且启用的原控件，否则使用明确入口回退，不引入全局焦点状态。#195 复用 assistant-ui 官方 External Store onCancel / Composer Cancel，AbortController 仅作用于本页 HTTP；取消递增现有请求序号，旧 finally 不清除新请求，既有消息和候选保留。
