## Purpose

让修复版本能够在不删除、不取消原有 stranded queued Execution 的条件下升级，精确区分真实运行清理责任与从未领取的占位状态，保护数据库、冻结输入、Broker 与私有恢复材料。

## ADDED Requirements

### Requirement: 显式候选绑定的责任保全升级
默认自动部署 SHALL 保留原空闲门禁。特定 Incident 升级 MUST 使用显式、候选 SHA/schema 绑定的私有清单，仅允许登记的 queued＋open Incident、无 active Attempt/Slot 的责任；其他活动或未知状态保持阻塞。不得通过取消旧queued、清空卷、修改业务lease/终态或永久忽略busy完成升级。

同 schema 后继 MUST 仅允许显式 `0040_issue152_dispositions → 0040_issue152_dispositions`，且 schema 对象集合保持完全相同；不得把任意相同 revision 视为兼容。新候选 MUST 生成新的 SHA/controller 绑定清单。规划及后续核验 MUST 要求 `execution_incident_dispositions` 表存在且为空，并原样保留既有 `runtime_reconciliation_cursors`，不得重新写入 seed。未知同 revision、未知向前路径或已有 disposition MUST 在 manifest 写出及停止服务前拒绝。

#### Scenario: 保留旧 queued Incident
- **WHEN** 精确候选通过原CI/历史/迁移门禁且清单资格满足
- **THEN** 停旧Control和Worker后再次核验无真实在途责任，保留原Execution/Incident/输入/Outbox/Admission并向前升级

#### Scenario: 预检之后发生 Claim
- **WHEN** 预检与停写复检之间产生新的Attempt或其他非清单责任
- **THEN** 迁移不开始，安全恢复旧服务等待或保留attention，不把先前检查当作当前事实

#### Scenario: 0040 同 schema 后继保持既有对象
- **WHEN** 当前和候选 revision 均为 `0040_issue152_dispositions`，责任满足现有分类，审计表为空且 schema inventory 与 cursor 行均未改变
- **THEN** 使用绑定新候选的 fresh manifest 继续全部保全门禁，不新增表且不重新初始化 cursor

#### Scenario: 未知 transition 或已有人工处置
- **WHEN** transition 不是登记的向前路径或显式 0040 同 schema 路径，或 `execution_incident_dispositions` 已有任一行
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
升级 SHALL 在停写后、备份后和迁移后启动前比较原Execution/Attempt/Slot/Incident/Outbox/Admission/Input Lease/Hold及journal材料的旧字段指纹，允许本变更新增对象，不改写旧责任。私有内容 MUST 不进入公共仓库；精确镜像、备份、Sandbox与真实执行探针门禁继续有效，失败不得自动downgrade。

#### Scenario: 升级后人工恢复原记录
- **WHEN** 新版本启动并通过真实UI/API处置旧Incident
- **THEN** 验收关联原ID、Attempt、代次、输出及资源释放，新建任务成功不能替代此证据

#### Scenario: 指纹或迁移失败
- **WHEN** 旧责任指纹不一致或迁移/验证失败
- **THEN** 保留现场、卷、备份和attention，不更新deployed成功状态或自动恢复旧schema
