## Purpose

定义从已查看的官方模板安全生成独立用户 Adapter 的原子合同，并确保实例化不会继承任何模板侧或演示运行绑定，成功后可可靠交给编辑工作区。

## ADDED Requirements

### Requirement: 专用实例化接口按所见版本创建
系统 SHALL 提供 `POST /api/templates/scenarios/{scenario_slug}/variants/{language}/instantiate`。请求体 MUST 仅允许 `name`、可选 `description` 和必填 `expected_template_version`；未知字段 MUST 被拒绝。名称 MUST 复用现有 Adapter 的 trim、非空、最大 128 字符和活跃名称唯一性规则。服务端 SHALL 从目录解析 language、Adapter 类型、代码和配置，客户端不得覆盖这些模板权威字段。

成功时接口 MUST 返回 HTTP 201、完整 `AdapterResponse`，并在 `Location` 指向 `/api/adapters/{id}`。未知场景、未知语言、版本不一致和名称冲突 MUST 分别返回稳定错误 `template_scenario_not_found`、`template_variant_not_found`、`template_version_conflict` 和 `adapter_name_conflict`。

#### Scenario: 按当前语言成功实例化
- **WHEN** 已认证用户提交有效名称、当前模板版本并从 Java Variant 发起实例化
- **THEN** 系统返回一个 language 为 `java` 的新 Adapter 和其 Revision 1
- **AND** 响应状态为 201 且 Location 指向新 Adapter

#### Scenario: 模板发布期间发生版本漂移
- **WHEN** `expected_template_version` 与服务端当前 Scenario 版本不一致
- **THEN** 系统返回 409 `template_version_conflict`
- **AND** 不创建任何 Adapter 或关联对象

#### Scenario: 客户端试图伪造权威字段
- **WHEN** 请求体额外包含 code、adapter_type、owner、worker、credential、schedule 或 run mode
- **THEN** 系统返回 422 且不创建任何对象

### Requirement: Adapter 与 Revision 1 在单事务中产生
实例化 SHALL 在一个数据库事务内创建 Adapter、必需的 Slot 0、类型所需的最小禁用配置、Revision 1，并把 `latest_version_id` 指向 Revision 1。任一步骤失败 MUST 回滚全部写入；不得先返回空 Adapter 再异步补代码。

Revision 1 的 code、requirements 和允许的非敏感 runtime config MUST 与用户所见模板版本及语言 Variant 精确一致。此专用系统事务 MAY 在未选择 Worker 时创建 Revision 1；现有普通 Save 的 Worker 前置规则 MUST 保持不变。

#### Scenario: 事务完整成功
- **WHEN** 模板实例化提交成功
- **THEN** 数据库中恰有一个新 Adapter、一个 Slot 0 和一个 seq=1 Revision
- **AND** Adapter 的 `latest_version_id` 指向该 Revision

#### Scenario: 中途失败完整回滚
- **WHEN** 创建 Slot、类型配置或 Revision 的任一步骤失败
- **THEN** Adapter、Slot、Input 配置、Webhook 配置和 Revision 均不存在
- **AND** 可使用同一名称重新尝试

### Requirement: 新 Adapter 默认停止且与模板解耦
新 Adapter SHALL 使用模板定义的 language、`task`/`webhook` 类型和描述，`run_mode` MUST 为 `manual`，`runtime_worker_id` MUST 为空，单次超时 MUST 使用当前平台安全默认值。新 Adapter MUST 没有运行中的 Execution，模板后续升级 MUST NOT 修改其代码、requirements、runtime config 或元数据。

#### Scenario: 模板升级不回写用户代码
- **WHEN** 用户实例化版本 1.0.0 后平台发布该 Scenario 的 1.1.0
- **THEN** 已有 Adapter 仍保留其 Revision 1 内容
- **AND** 不自动创建新 Revision 或修改 latest_version_id

#### Scenario: 实例化后尚不可运行
- **WHEN** 用户尚未选择 Worker 或配置真实运行条件
- **THEN** Adapter 保持停止且不会产生 Execution、Admission、Outbox 或 Attempt

### Requirement: 实例化不继承运行绑定或历史
模板实例化 MUST 使用专用语义，MUST NOT 调用或等价复现普通 Clone 的继承行为。事务不得创建或绑定 Credential、Credential Binding、Dependency 实体或安装状态、Managed File、Artifact/Lease、Worker、Schedule、额外 ACL/权限、Execution、Admission、Outbox、Attempt 或历史记录；即使部署中存在演示 Credential，也不得自动绑定。

Task Adapter SHALL 仅获得现有模型要求的空或模板允许的非敏感输入配置。Webhook Adapter SHALL 获得全新、禁用的 Webhook 配置和全新 public id，credential 必须为空；不得复制模板 Token、路径或接收启用状态。

#### Scenario: 存在演示 Credential 时实例化 Task
- **WHEN** 部署已有默认演示 Credential 且用户从 Task 模板实例化
- **THEN** 新 Adapter 的 Credential Binding 数量为 0
- **AND** 其他运行态对象计数不因实例化增加

#### Scenario: 实例化 Webhook 模板
- **WHEN** 用户实例化 Webhook JSON 标准化模板
- **THEN** 新 Webhook 配置为 disabled、credential 为空且 public id 为全新值
- **AND** 不继承任何模板或其他 Adapter 的 Token、public id 或启用状态

### Requirement: 所有权与权限沿用身份边界
实例化接口 SHALL 要求现有业务主体认证和现有账户态 CSRF 防护。具有账户用户记录的调用者（包括账户管理员）SHALL 成为新 Adapter 的 `owner_user_id`；无账户用户记录的部署 superadmin 创建时 SHALL 保持现有 system-owned 语义。不得创建模板维护者权限或额外共享关系。

#### Scenario: 普通账户用户拥有实例化结果
- **WHEN** 账户用户成功实例化模板
- **THEN** 新 Adapter 的 owner_user_id 等于该账户用户 id
- **AND** 用户获得现有 owner 编辑能力

#### Scenario: 未认证或缺少 CSRF
- **WHEN** 未认证请求或缺少要求的账户态 CSRF 保护发起 POST
- **THEN** 系统拒绝请求且不创建任何对象

### Requirement: 来源字段只读且成对保存
Adapter SHALL 新增只读、可空的 `template_scenario_slug` 与 `template_version`。从模板实例化时两者 MUST 同时写入；普通新建和历史 Adapter 两者 MUST 同时为空。普通 Adapter 创建、更新、保存版本和 Clone API MUST NOT 接受客户端写入或修改这两个字段，且数据库 MUST 防止只填写其中一个。

#### Scenario: 来源审计可见但不可编辑
- **WHEN** 用户读取模板实例化的 Adapter
- **THEN** AdapterResponse 返回来源 slug 与实例化时版本
- **AND** 更新 Adapter 元数据不会改变这两个字段

#### Scenario: 旧 Adapter 保持兼容
- **WHEN** 数据库从旧版本迁移
- **THEN** 所有既有 Adapter 的两个来源字段均为空
- **AND** 既有 API 客户端无需提供新字段

### Requirement: 并发名称冲突仅产生一个完整对象图
名称预检查 MAY 用于用户体验，但数据库唯一约束 SHALL 是并发权威。同名并发实例化 MUST 只有一个请求成功；另一请求 MUST 返回 409 `adapter_name_conflict`。除精确名称唯一约束外的完整性错误 MUST 回滚并按真实错误处理，不得伪装成名称冲突。

#### Scenario: 两个请求竞争同名 Adapter
- **WHEN** 两个已同步到同一提交点的请求并发实例化同一名称
- **THEN** 恰有一个返回 201，另一个返回 409
- **AND** 数据库中仅存在一套完整 Adapter、Slot、类型配置和 Revision 1

### Requirement: Web 成功后自动进入新 Adapter 编辑页
Web 客户端 SHALL 在发出 POST 前完成现有工作区离开确认；用户取消时不得发出实例化请求。POST 成功后客户端 SHALL 刷新 Adapter 列表，选中响应中的 Adapter，加载其 Revision 1，切换至 `/adapters` 的编辑工作区并激活编辑标签。若 Adapter 已创建但后续 Revision 加载失败，客户端 MUST 仍显示真实的新 Adapter 和加载错误，不得把已提交的创建伪装成未发生。

#### Scenario: 复制命名后直接编辑
- **WHEN** 用户确认名称且实例化成功
- **THEN** 页面自动从模板详情进入新 Adapter 的编辑页
- **AND** 编辑器显示所选语言的 Revision 1 代码，Adapter 状态显示为停止

#### Scenario: 用户取消丢弃旧草稿
- **WHEN** 当前 Adapter 存在未保存内容且用户在实例化前取消离开确认
- **THEN** 客户端不发送 POST
- **AND** 保留模板详情、名称输入和原工作区草稿
