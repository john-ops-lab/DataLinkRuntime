## Purpose

为 AI 只读工具建立可关联、可脱敏和有界保存的审计轨迹，使成功、失败及保护性停止都可复盘，同时避免日志泄密或长期占满宿主机存储。

## ADDED Requirements

### Requirement: 请求与会话关联标识
每次 AI Assist 请求 MUST 获得服务端生成的唯一 `request_id`。Web 对一次浏览器内 AI 会话 SHALL 生成不含用户内容的随机 `conversation_id` 并在该会话的每次 Assist 中复用；为兼容旧客户端，该字段 MUST 为可选，缺失时服务端 SHALL 生成仅覆盖当前请求的替代关联标识。工具审计记录 MUST 同时包含这两个标识及 Adapter 标识。

#### Scenario: 同一会话连续提问
- **WHEN** 用户在同一个 AI 面板会话中连续发起两次 Assist
- **THEN** 两次请求 MUST 具有不同的 `request_id` 和相同的 `conversation_id`，其工具事件可按会话和请求分别关联

#### Scenario: 旧客户端未发送会话标识
- **WHEN** 兼容客户端提交不含 `conversation_id` 的现有请求
- **THEN** 服务端 MUST 接受请求并为审计生成替代标识，不得破坏现有请求合同

### Requirement: 每次工具尝试均持久留痕
系统 MUST 在每次工具调用成功、失败或被重复 / 预算 / 时限保护拦截时，立即写入一条结构化审计事件。事件白名单 MUST 包含时间、`request_id`、`conversation_id`、Adapter 标识、轮次、调用序号、工具名、脱敏且有长度上限的参数摘要、状态、耗时、结果大小、截断标记、稳定错误码或停止原因；不得依赖最终 Assist 成功后才批量写入。

#### Scenario: 工具成功后后续 Provider 失败
- **WHEN** 一个工具调用成功并写入事件，但随后的 Provider 请求失败
- **THEN** 已完成调用的审计事件 MUST 仍保留，且失败终止事件 MUST 使用同一 `request_id`

#### Scenario: 工具自身失败
- **WHEN** 工具因参数、超时、外部源或执行错误失败
- **THEN** 系统 MUST 写入错误状态、耗时和稳定错误码，且不得遗漏该调用

#### Scenario: 预算或循环保护触发
- **WHEN** 调用因预算、重复、连续失败或总时限保护未被执行
- **THEN** 系统 MUST 写入带明确停止原因的拦截事件，并保留此前同一请求的全部事件

### Requirement: 审计内容最小化与脱敏
审计日志 MUST 只保存白名单元数据。Secret、Credential 真值、平台 Token、API Key、Cookie、完整用户 Prompt、附件正文、Adapter 源码、完整工具结果、Provider reasoning 和原始响应 MUST NOT 写入审计日志；参数摘要 MUST 在落盘前完成密钥值替换、结构裁剪和确定性长度限制。

#### Scenario: 参数或结果包含敏感值
- **WHEN** 工具参数、外部结果或错误中包含已知 Credential、API Key 或 Secret 值
- **THEN** 审计文件中 MUST 只出现脱敏占位，不得出现敏感值或可还原的完整内容

#### Scenario: 审计敏感字段集合
- **WHEN** 检查一次包含对话、附件和工具结果的完整 Assist 审计文件
- **THEN** 文件 MUST 只包含白名单元数据，不得包含 Prompt、附件正文、完整结果、reasoning 或原始响应

### Requirement: 工具审计日志有界轮转
工具审计日志 MUST 写入 Control 的持久平台日志边界，并由应用执行基于文件大小和保留文件数量的轮转；默认单文件上限 SHALL 为 10 MiB，默认最多保留当前文件和 10 个历史文件。部署配置 MUST 要求正数且不得提供无限保留值，达到上限时 MUST 删除最旧轮转文件而不影响当前写入。

#### Scenario: 当前文件达到大小上限
- **WHEN** 写入下一条完整审计事件会越过单文件大小上限
- **THEN** 系统 MUST 先轮转当前文件、在新文件写入完整事件，并保持可关联的 JSON Lines 记录边界

#### Scenario: 历史文件超过保留数量
- **WHEN** 一次轮转会使历史文件数量超过配置上限
- **THEN** 系统 MUST 只删除最旧的审计轮转文件，保留当前文件和配置数量内的最新历史文件

#### Scenario: 支持的 Compose 部署重启
- **WHEN** Control 重启且平台日志 bind mount 仍存在
- **THEN** 新工具事件 MUST 继续写入有界审计文件，不得因重启重置为无界日志或写入 Docker 存储卷之外的临时位置

### Requirement: 截断结果仅以省略号呈现
工具结果的服务端安全长度边界、`result_truncated` 状态和结果大小统计 MUST 保持生效。当前端展示已截断的工具结果摘要时，可见摘要 MUST 以单个 Unicode 省略号 `…` 结尾，并 MUST NOT 展示“结果过大”“已截断”、内部截断标记或对应的额外可访问提醒。

#### Scenario: 中文界面展示截断结果
- **WHEN** 服务端返回 `result_truncated=true` 且系统语言为 `zh-CN`
- **THEN** 工具卡片 MUST 显示以 `…` 结尾的有界摘要，且页面和无障碍树中均不得出现额外截断提示

#### Scenario: 英文界面展示截断结果
- **WHEN** 服务端返回 `result_truncated=true` 且系统语言为 `en`
- **THEN** 工具卡片 MUST 显示同样的省略号结尾规则，且不得显示英文截断提示

#### Scenario: 未截断结果
- **WHEN** 服务端返回 `result_truncated=false`
- **THEN** 工具卡片 MUST 按现有安全摘要合同展示内容，不得无故追加省略号
