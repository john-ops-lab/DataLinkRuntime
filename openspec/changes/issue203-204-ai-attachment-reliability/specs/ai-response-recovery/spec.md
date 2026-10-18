## ADDED Requirements

### Requirement: Bounded strict output correction
系统 SHALL 按实际模型、端点和协议选择能力，保持严格 JSON、Schema、Unicode 与 Secret reflection 验证；最终格式或 Schema 错误最多纠正一次，共用总时限与上下文预算，不切换 Provider。

#### Scenario: Format correction
- **WHEN** 首次最终 JSON 损坏或 Schema 不匹配且仍有预算
- **THEN** 记录脱敏首败阶段，以原冻结请求最多发起一次无工具纠正；再次失败给稳定错误

#### Scenario: Safety failure
- **WHEN** 输出含 Secret、无效 Unicode、配置越权或请求取消/权限变化
- **THEN** 不执行格式纠正

### Requirement: Current attachment recovery
系统 SHALL 在 Provider 调用前拒绝坏附件。新一轮只携带当前附件，Retry/Regenerate 复用原冻结请求；迟到回复仍受 scope/generation fencing 约束。

#### Scenario: New request after rejection
- **WHEN** 同会话坏附件拒绝后换正常 TXT 新发送
- **THEN** 正常 TXT 独立处理，无刷新或新会话要求，未调用 Provider 的坏文件不进入材料历史

#### Scenario: Transport failure recovery
- **WHEN** Provider 连接失败后恢复
- **THEN** 界面结束发送状态，显示重试原请求与换文件新发送入口，后续同会话请求可继续

### Requirement: Sanitized diagnostics
审计 SHALL 只保存固定失败阶段、输出策略、耗时与纠正次数，不保存 Prompt、Response、reasoning 或敏感材料。

#### Scenario: Failed correction
- **WHEN** 纠正仍失败
- **THEN** 脱敏审计可区分首败与纠正结果，原始输出不持久化
