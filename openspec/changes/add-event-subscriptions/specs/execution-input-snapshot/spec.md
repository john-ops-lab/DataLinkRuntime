# Spec Delta

## ADDED Requirements

### Requirement: 订阅执行使用独立触发身份和现有 JSON 输入协议
事件 Execution SHALL 使用 `trigger=subscription` 和 `input_source_type=json`，输入为 `dlr.event.v1` Envelope。公开 input_snapshot MUST 保持 `{source_type: json, revision: null}`，不增加文件字段；订阅配置版本和源身份由 Envelope 与追踪关联表达。

#### Scenario: 创建事件 Execution
- **WHEN** 一条已接管事件通过 Admission 创建执行
- **THEN** API、筛选、历史与 Worker 一致识别 subscription trigger，JSON 输入可按普通 Adapter 协议读取，不误用 Task InputConfig revision

### Requirement: 事件输入和执行目标在创建时不可变
首次 Execution SHALL 使用原接管激活引用中的 Revision、Worker 和凭据绑定引用，并固定完整输入及既有执行策略快照；新补处理仅可按显式 replay/repair 合同选择版本。来源重投、后续合并与配置保存 MUST 不覆盖已有 Execution。凭据引用快照不得被描述为冻结 Secret 真值。

#### Scenario: 旧 pending 在新代码保存后被派发
- **WHEN** 通过受限修复保存了新代码，而旧 pending 尚未转执行
- **THEN** 旧 pending 仍使用其原激活版本，只有明确指定修复版本的新补处理可使用修复代码

#### Scenario: 同对象变化发生在执行期间
- **WHEN** K8s 新变化在现有 Execution 运行时接收
- **THEN** 原 Execution 输入保持不变，后续变化由独立责任承担

### Requirement: 事件追踪与历史只读边界分开
系统 SHALL 提供事件到原 Execution、Attempt 和补处理执行的关联；普通 Execution 历史继续只读。replay MUST 从独立受权限保护的订阅动作发起，不提供可操作的文件 Lease、内部存储路径或下载凭据。

#### Scenario: 从历史查看重放链
- **WHEN** 用户打开已经重处理的事件历史
- **THEN** 能区分原执行终态和新执行结果，不能通过普通历史读取请求创建运行或改变输入
