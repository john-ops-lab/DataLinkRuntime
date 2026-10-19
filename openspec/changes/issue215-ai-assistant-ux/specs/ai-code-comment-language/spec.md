# Spec Delta

## Purpose

让 AI 生成的适配器代码在不额外要求注释时仍能被管理员理解和维护。以服务端系统配置为语言权威，在所有代码建议路径采用一致的中文或英文注释，同时保留目标语言语法、代码完整性和现有人工审查契约。

## ADDED Requirements

### Requirement: Sufficient localized comments in code suggestions

系统 SHALL 默认要求所有代码建议在实际候选代码内充分、准确加注释，解释用途、输入输出、字段映射、关键步骤、重要判断和异常处理，并说明不直观实现的原因和约束。中文系统 SHALL 使用中文注释，英文系统 SHALL 使用英文注释；用户消息语言、附件和历史对话不得改变此权威。注释 SHALL 使用目标语言语法，保留技术标识且与逻辑一致，避免逐行机械复述。

#### Scenario: Opposite-language request
- **WHEN** 中文系统收到英文代码修改请求，或英文系统收到中文请求
- **THEN** 交付的完整候选代码仍采用系统配置语言的充分注释

#### Scenario: Reuse generation paths
- **WHEN** 发生首次生成、修复、重新生成或附件驱动生成，或系统语言变更后的新请求
- **THEN** 同一系统规则生效，更新已有注释以匹配本次逻辑，且代码完整并符合当前运行契约

#### Scenario: Translate preserved explanatory comments
- **WHEN** 修复或重新生成的原代码包含与当前系统语言相反的有效说明注释
- **THEN** 保留说明原意并译成当前系统语言，保持编译指令及技术标识不变，并核对注释声明与实际运算符及库行为一致

#### Scenario: Localize copied comments during a small edit
- **WHEN** 用户仅要求一处业务改动，而当前完整代码中已有与系统语言相反的说明注释或文档字符串
- **THEN** 完整候选中的复制注释同样视为本次输出，其说明按当前系统语言本地化；该要求优先于注释文字的最小改动，但不授权改变无关业务逻辑或数据字面量
