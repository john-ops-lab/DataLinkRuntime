## Purpose

定义 AI 可检索 Java Runtime 说明与实际 Context 的一致性，确保文档引导的代码使用现有公共字段和日志 API，并由真实编译运行验证配置、输出、日志和凭据引用，避免为错误文档增设 Runtime 方法。

## ADDED Requirements

### Requirement: Java 文档以实际 Runtime 字段为准
Java AI 检索内容、配置说明和相关示例 SHALL 使用 `context.config`、`context.secrets.get(...)`、`context.logger.info/warn/error`；MUST 不以新增 Runtime 方法迁就错误说明。

#### Scenario: 读取与搜索 Java 合同
- **WHEN** AI 工具检索或读取 Java Runtime/配置说明
- **THEN** 返回实际字段访问和正确 logger 方法，不出现 config()/secrets()/logger() 或错误 warning 方法式合同

#### Scenario: 关联示例一致
- **WHEN** 检查相关 Java 文档与示例
- **THEN** 同一 Context API 的表述一致，其他语言合同保持各自实际 Runtime 语义

### Requirement: 示例验证真实编译和执行
代表性 Java 示例 SHALL 对实际 Runtime 源码编译并运行，验证配置读取、JSON 输出、日志以及合成凭据存在性；MUST 不输出凭据值，字符串断言或单次模型成功不替代该证据。

#### Scenario: 文档示例编译运行
- **WHEN** 将可检索文档中的代表性示例与当前 Java Runtime 编译并执行
- **THEN** 配置读取和输出符合独立预期，info/warn/error 日志可见，凭据仅用于存在性或合成业务判断且正文/日志不含真值

#### Scenario: 后续 Prompt 使用
- **WHEN** 后续 #128 重组 Prompt/文档资源
- **THEN** 继续复用已纠正的契约；Java/Go Provider 首次 ai_response_invalid 仍独立归因
