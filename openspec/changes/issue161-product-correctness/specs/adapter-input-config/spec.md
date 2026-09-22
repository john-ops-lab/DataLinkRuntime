## ADDED Requirements

### Requirement: JSON 保存保持类型与事务镜像一致
系统 SHALL 在真实 PostgreSQL 提交后保留 JSON 值的类型和值，数字与布尔值 MUST 被区分；成功保存仍遵守现有 revision 递增规则，失败保存不得改变当前值、revision 或 Schedule 兼容镜像。

#### Scenario: 数字与布尔双向保存
- **WHEN** 用户依次保存顶层、对象成员或数组元素的 `0 ↔ false`、`1 ↔ true`
- **THEN** 提交后新数据库 Session、读取 API 与页面重载均返回本次保存的类型和值，存在的 Schedule 兼容镜像与之相同

#### Scenario: 保留空值与重复保存合同
- **WHEN** 用户保存 JSON null、切换无输入、保存普通数字/字符串或再次保存完全相同 JSON
- **THEN** JSON 来源持久化 JSON null、无 JSON 来源保留 SQL NULL，所有合法保存继续按既有规则递增 revision，不引入去重或历史回填

#### Scenario: 已创建执行不被改写
- **WHEN** 输入更新发生在旧 Execution 创建后，新 Execution 创建前
- **THEN** 新 Execution 读取新配置，旧 Execution 输入快照、输出和历史摘要保持原样

#### Scenario: 兼容路径与冲突事务
- **WHEN** 旧 Schedule 输入写入通过统一配置路径，或任一路径遇到 revision/runtime lock/字段校验冲突
- **THEN** 成功写入的当前 JSON 与兼容镜像原子一致，失败事务不留下部分更新
