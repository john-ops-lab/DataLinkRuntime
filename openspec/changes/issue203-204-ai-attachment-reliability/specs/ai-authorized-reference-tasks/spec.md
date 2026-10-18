## ADDED Requirements

### Requirement: Authorized reference requirements
系统 SHALL 按用户本轮明确授权采用附件的业务字段、映射、约束和返回要求，允许生成完整替换 Candidate；Working Copy 仅为当前代码事实来源。附件 MUST NOT 获得系统指令权限。

#### Scenario: Excel business task
- **WHEN** 用户要求根据当前 XLS 的 asset_label 和 ASSET-XLS-7391 生成代码
- **THEN** Prompt 明确允许采用这些需求，Candidate 可替换当前业务逻辑，仍须人工 Apply/Save/Run

#### Scenario: Mixed injection
- **WHEN** 正常业务材料混有泄露 Secret 或越权工具命令
- **THEN** 正常业务资料仍可采用，越权内容不获授权

### Requirement: Business completion is separate
系统 SHALL 独立于技术响应 success 返回代码任务完成状态；代码任务没有 Candidate 为 incomplete，咨询允许 candidate=null。

#### Scenario: Valid refusal
- **WHEN** 代码任务得到有效解释但无 Candidate
- **THEN** 技术响应成功、业务 incomplete，并保留原因，不伪称协议失败

#### Scenario: Consultation
- **WHEN** 用户仅要求解释正常 TXT 字段
- **THEN** 无 Candidate 的答复为 answered
