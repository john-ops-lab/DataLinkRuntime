## MODIFIED Requirements

### Requirement: 输入来源具有封闭类型合同
输入配置 SHALL 支持 `none`、`json`、`managed_files`、`remote_files` 四个枚举值；第一阶段 `none`、`json`、`managed_files` 可保存，`remote_files` MUST 被后端拒绝为 `input_source_not_available`。

#### Scenario: 保存无输入
- **WHEN** 客户端以当前 expected revision 保存 `source_type=none` 且不携带类型专属字段
- **THEN** 系统保存配置、递增 revision，并报告 `valid_for_run=true`

#### Scenario: 保存任意 JSON 顶层值
- **WHEN** 客户端保存 `source_type=json` 并显式携带 object、array、scalar 或 JSON `null`
- **THEN** 系统仅移除字符串值及对象键中的真实 U+0000，保存其余 JSON 值并报告可运行

#### Scenario: 拒绝类型不相容字段
- **WHEN** 请求携带与 `source_type` 不相容的 `json_value`、`artifact_ids` 或 retention 字段
- **THEN** 系统返回 422 校验错误且不改变最近一次有效配置

#### Scenario: 远端文件占位不可保存
- **WHEN** 客户端尝试保存 `source_type=remote_files`
- **THEN** 系统返回稳定 code `input_source_not_available` 且 revision 不变

## ADDED Requirements
### Requirement: JSON 输入规范化反馈与结构保护
系统 SHALL 递归移除真实 U+0000 并返回实际保存值和移除数量；MUST 保留字面反斜杠 u0000、其他字符、所有 JSON 类型。键清理冲突 MUST 返回可定位错误且不改变输入、revision、绑定或历史。直接 API、Schedule 兼容写入和 Adapter 导入 SHALL 复用规则、现有事务、权限、锁及大小限制。
#### Scenario: 嵌套与标量保存
- **WHEN** 输入含顶层字符串或嵌套数组、对象键、多个空字符、0/false、1/true、null
- **THEN** 仅空字符被移除，数量准确，重复规范化不改变值；JSON null 与无 JSON 的 SQL NULL 保持区别
#### Scenario: 前端预清理保留原始大小门禁
- **WHEN** JSON 请求携带可选非负整数 original_json_size_bytes 或未声明
- **THEN** 声明只能提高大小检查量，服务仍校验实际入参大小；其他来源拒绝该字段
#### Scenario: 对象键冲突
- **WHEN** 同一对象中的两个键移除空字符后相同
- **THEN** 返回键路径冲突且整体保存失败，不覆盖字段
### Requirement: Task 保存反馈采用服务端权威值
Web SHALL 先解析 JSON 后规范化本次提交并反馈清理数量，成功时采用服务端返回实值。MUST 保留新草稿及错误草稿，不允许迟到响应覆盖更新草稿。
#### Scenario: 保存成功
- **WHEN** 含空字符草稿保存成功
- **THEN** 中英文提示准确数量且编辑框采用服务端值，无清理时沿用普通成功反馈
#### Scenario: 保存期间继续编辑
- **WHEN** 提交后用户更新草稿或切换请求范围
- **THEN** 迟到结果不得覆盖新草稿
