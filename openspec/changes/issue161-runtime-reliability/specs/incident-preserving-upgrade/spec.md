## Purpose

让修复版本能够在不删除、不取消原有 stranded queued Execution 的条件下升级，精确区分真实运行清理责任与从未领取的占位状态，保护数据库、冻结输入、Broker 与私有恢复材料，并以独立受限模式保全已完成处置的审计和终态以交付本组 Web 可读性修复。

## ADDED Requirements

### Requirement: 显式候选绑定的责任保全升级
默认自动部署 SHALL 保留原空闲门禁。特定 Incident 升级 MUST 使用显式、候选 SHA/schema 绑定的私有清单；原 manifest v2 仅允许登记的 queued＋open Incident 与合法 cleanup、无 active Attempt/Slot 的责任；其他活动或未知状态保持阻塞。不得通过取消旧queued、清空卷、修改业务lease/终态或永久忽略busy完成升级。

原 v2 的同 schema 后继 MUST 仅允许显式 `0040_issue152_dispositions → 0040_issue152_dispositions`，且 schema 对象集合保持完全相同；不得把任意相同 revision 视为兼容。新候选 MUST 生成新的 SHA/controller 绑定清单。v2 规划及后续核验 MUST 要求 `execution_incident_dispositions` 表存在且为空，并在原 inventory 中保留既有 `runtime_reconciliation_cursors` 表，不得重新执行 0039 seed；当前游标不要求等于初始 `0/0`，应用运行期间正常 reconciler MAY 推进游标。未知同 revision、未知向前路径或 v2 下已有 disposition MUST 在 manifest 写出及停止服务前拒绝。

#### Scenario: 保留旧 queued Incident
- **WHEN** 精确候选通过原CI/历史/迁移门禁且清单资格满足
- **THEN** 停旧Control和Worker后再次核验无真实在途责任，保留原Execution/Incident/输入/Outbox/Admission并向前升级

#### Scenario: 预检之后发生 Claim
- **WHEN** 预检与停写复检之间产生新的Attempt或其他非清单责任
- **THEN** 迁移不开始，安全恢复旧服务等待或保留attention，不把先前检查当作当前事实

#### Scenario: 0040 同 schema 后继保持既有对象
- **WHEN** 当前和候选 revision 均为 `0040_issue152_dispositions`，责任满足现有分类、审计表为空且 schema inventory 未改变
- **THEN** 使用绑定新候选的 fresh manifest 继续全部保全门禁，不新增表且不重新初始化 cursor

#### Scenario: v2 的未知 transition 或已有人工处置
- **WHEN** v2 的 transition 不是登记的向前路径或显式 0040 同 schema 路径，或 `execution_incident_dispositions` 已有任一行
- **THEN** 在写出 manifest 和停止服务前拒绝规划，不清除审计、不回滚业务状态也不忽略门禁

### Requirement: cleanup 以真实责任分类且不伪造完成
从未Claim的记录 SHALL 只有在Attempt、worker/start字段及工作区/journal/进程证据一致为空时派生cleanup不适用，MUST 保留原字段；有历史Attempt的deferred清理只有在原journal及持久卷可验证、没有活动进程时可保全，后续由真实清理receipt收敛。未知状态 MUST 阻塞。

#### Scenario: 无 Attempt 的 pending 占位
- **WHEN** queued或合法terminal记录满足完整never-claimed证据
- **THEN** 不把pending当成运行进程，不写completed，不要求删除记录或人为补Incident

#### Scenario: 历史 deferred 和丢失 journal
- **WHEN** 历史terminal Attempt仍有deferred cleanup
- **THEN** 完整可验证journal随原卷保留并由Worker恢复；journal缺失/残留未知时继续阻塞，不能伪造receipt

### Requirement: 运行责任在迁移前后可核对
升级 SHALL 在停写后、备份后和迁移后启动前比较原Execution/Attempt/Slot/Incident/Outbox/Admission/Input Lease/Hold及journal材料的旧字段指纹，仅对登记的向前迁移允许本变更新增对象，同 schema 不允许增删对象或改变被保护表的列/PK，不改写旧责任。私有内容 MUST 不进入公共仓库；精确镜像、备份、Sandbox与真实执行探针门禁继续有效，失败不得自动downgrade。

#### Scenario: 升级后人工恢复原记录
- **WHEN** 新版本启动并通过真实UI/API处置旧Incident
- **THEN** 验收关联原ID、Attempt、代次、输出及资源释放，新建任务成功不能替代此证据

#### Scenario: 指纹或迁移失败
- **WHEN** 旧责任指纹不一致或迁移/验证失败
- **THEN** 保留现场、卷、备份和attention，不更新deployed成功状态或自动恢复旧schema


### Requirement: 已完成处置仅可显式进入受限 Web 同 schema 模式
系统 SHALL 仅为本组详情输出可读性修复提供独立 `audited-web-same-schema-v1` / manifest v3，MUST 显式选择且旧/新 schema 均精确为 `0040_issue152_dispositions`、完整迁移图不变。无 mode 的入口和原 v2 MUST 保持 audit-empty 规则，不能因存在审计自动升级到 v3。v3 的版本、mode、顶层键和选择字段 MUST 为闭合形状；未知或混用合同在写 manifest 和停止服务前拒绝。

私有选择 MUST 显式列出所有待保留 queued、fresh cleanup 与已经完成的 terminal 关联，并为每个 terminal 提供独立的精确 status、generation、输出摘要、错误码和 Attempt 数预期。现场 ID、数量、地址和私有路径 MUST 不成为公共代码或合同常量，任何记录不得通过通配选择或回写当前值生成无条件成立的预期。

#### Scenario: 已完成处置后的合法 Web 修复
- **WHEN** 操作者显式选择 v3，独立终态预期与全部责任/审计关系满足合同，且候选只含允许的本组差异
- **THEN** 生成绑定当前事实与新候选的 fresh manifest，保留既有审计和终态，继续全部正式升级门禁

#### Scenario: 未选择 v3 却已有审计
- **WHEN** 使用默认模式或 v2 清单，审计表中已有任何行
- **THEN** 在停止服务前拒绝，既不自动转换 v3，也不清除审计或把原终态改回 queued

#### Scenario: 未知模式或跨 schema
- **WHEN** v3 mode/schema/清单形状不匹配，或试图重绑候选/控制器
- **THEN** 规划和切换均拒绝，不把任意同 revision 或带审计向前迁移视为受支持

### Requirement: 候选全树差异必须在闭合 Web 修复范围
v3 SHALL 核验旧部署 commit 至候选 commit 的全部 Git tree 差异，产品源差异 MUST 恰为 `web/src/index.css` 的已审查滚动修复；其他变化只允许 design 第 9.1 节逐文件列出的既有回归、控制器和规划/操作合同。路径规则 MUST 为固定精确集合，拒绝未知路径、目录通配、新增/删除/重命名/类型或 mode 变化、symlink 与 submodule。不得改 Backend、Worker、migration、依赖锁、Docker/Compose、CI 或其他产品源。

plan、select、switch 停止任何服务前 SHALL 从可信源缓存的真实 commit/tree/blob 重算完整差异和摘要，MUST 绑定旧/新 SHA、迁移图、controller、镜像和原存储事实；manifest 中的声明或布尔不得代替实际对象验证。所有候选镜像仍正式构建和核验。

#### Scenario: 允许路径中夹带其他产品修改
- **WHEN** 已有允许的 CSS 修改，但同一候选另改 Backend、依赖锁、CI、未知路径或文件类型
- **THEN** 整个候选在规划及切换时拒绝，不因 Web 子树检查通过而放行

#### Scenario: 规划后源对象或候选变化
- **WHEN** manifest 生成后候选、controller、完整 diff/tree 摘要或 migration graph 不再匹配
- **THEN** 在停止服务前拒绝并要求 fresh plan，不重写已封存清单的绑定

### Requirement: 全部审计列与已完成关系必须精确保全
v3 SHALL 在同一只读一致性事务捕获原十三张责任表外的全部 `execution_incident_dispositions`，MUST 包括实际十七列、实际 PK、按 PK 排序的整行摘要与总数。列/PK、幂等键、请求摘要、前后 Outbox 引用和所有其他字段均为保全内容；缺列/增列、同数换行、类型或值漂移、额外/缺失行不能以 count 或窄 observer 投影判为相同。

全表 audit ID 集合 MUST 精确等于显式 terminal 选择的处置集合，且每项只关联对应原 Execution/Incident。系统 SHALL 重验 actor、幂等/请求身份、原因、代次与前后 Outbox 的闭合关系，只接受已完成的 recovery_dispatched 或 canonical zero-Attempt cancellation；未知、拒绝、在途或未关联审计 MUST 阻塞。recover receipt 的历史 `execution_status=queued` SHALL 保持，与当前 succeeded/dead_letter 区分；不得为了同 schema 验证改写审计历史。

#### Scenario: 同数换行或隐藏审计列变化
- **WHEN** 审计总数不变，但 ID、idempotency_key、request_hash、Outbox 引用、时间或其他列发生变化
- **THEN** 以完整行证据拒绝，保留原数据和 attention，不接受窄投影一致作为通过

#### Scenario: 恢复 receipt 与当前终态不同
- **WHEN** 恢复审计为 recovery_dispatched、历史状态 queued、from/to 代次和 Outbox 关系正确，当前原 Execution 已达到明确预期终态
- **THEN** 原 receipt 与终态同时保留；不要求把历史 queued 写成当前 succeeded/dead_letter

#### Scenario: 合法取消保留已 published Outbox
- **WHEN** 零 Attempt 原 Execution 已 canonical cancelled，审计为 execution_terminal/execution_cancelled 且关联同代同一 published 原 Outbox
- **THEN** 保留原 Outbox 完整投影，不要求覆写 last_error_code 为取消 marker；Execution 与 audit 的 canonical code 继续精确验证

#### Scenario: 存在未选或尚在途处置
- **WHEN** 出现新审计、未选审计、额外拒绝 receipt、cancellation_requested、关系不闭合或仍活动的 terminal 目标
- **THEN** 规划或后续复查立即拒绝，不删除审计或将责任视为已完成

### Requirement: 终态保全不能隐藏 queued 与 cleanup 责任
v3 SHALL 匹配各原终态的明确预期并保留 ID、generation、输出/错误、历史 Attempt 和全部原行摘要，MUST 确认无 active Attempt/Slot、无 replay、已释放终态 Admission 且无未释放 Input Lease/Hold。剩余 queued 的全部原资格继续有效；Adapter/global count/bytes MUST 与仍承担的全库责任一致，不因同 Adapter 存在终态而归零。

terminal 与 cleanup 集合 SHALL 允许交集，queued MUST 与二者不交；terminal 的 pending/deferred 责任仍须显式列入 fresh cleanup 并经过原文件/kernel 证据分类，不能由 terminal 选择隐式豁免。停止 Control 后、完全停写后、备份后和同 head 核验后启动前 SHALL 重验十四表、完整表 inventory/列/PK、资产/文件/kernel/namespace/FD/卷身份；任何漂移 MUST 保持 attention 与原现场，不自动 downgrade、restore、reset cursor 或恢复写入。

#### Scenario: 已取消零 Attempt 仍是 pending 占位
- **WHEN** 已选 terminal 实际 cleanup=pending、零 Attempt 且无原 Claim/工作区/journal/进程事实
- **THEN** 只有也被 fresh cleanup 明确选中并通过真实空证据时才派生 not_applicable，原 pending 字段不变；漏选时拒绝

#### Scenario: 同 Adapter 仍有其他 queued
- **WHEN** 一个 Adapter 同时含所选已完成 terminal 和其他保留 queued
- **THEN** 只验证终态责任已正确释放，保留其余 queued 的 count/bytes，Admission 不被粗暴归零

#### Scenario: 计划与停写之间新增责任
- **WHEN** plan 通过后出现新 Claim、审计、cleanup 或任何所保全字段变化
- **THEN** 停写后的当前事实使切换失败，保留应用停止和 attention，不沿用旧快照继续

#### Scenario: 正式 probe 与原终态验收分开
- **WHEN** 新版本通过正式镜像/Sandbox/Broker/cleanup probe 并达到 Ready
- **THEN** 单独封存原 terminal/queued/审计和保留责任仍满足合同，probe 仅允许可关联的自身增量；真实 Chrome 只读检查原输出/日志，不再次处置或以新任务成功替代原记录可见性
