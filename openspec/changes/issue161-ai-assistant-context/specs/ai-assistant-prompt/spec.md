## Purpose

规定 DLR 内嵌 AI Assistant 如何从已验证的请求与服务端事实组装消息、隔离非可信材料，并在保持现有人工确认和严格输出边界的前提下，按本轮实际能力提供适当的回答与代码候选。

## ADDED Requirements

### Requirement: 随版本发布且可核验的静态规则
系统 SHALL 从随应用发布的三个只读 UTF-8 规则资源加载稳定行为、Adapter 边界和工具规则；资源 MUST 是纯静态文本，不能含动态占位符或执行模板替换。缺失、空文件或无效编码 MUST 在启动或模块初始化时明确失败。静态规则 SHALL 有内容 revision；组装结果的消息序列及其嵌套内容 MUST 不可变，诊断 SHALL 使用不可变类型且只包含非敏感 revision、Section 名称、system/context 大小和能力/材料开关。

#### Scenario: 更换工作目录与安装包运行
- **WHEN** 在不同工作目录运行已安装 wheel、sdist 构建或容器中的 Control
- **THEN** 三个资源均可读取，revision 稳定，Prompt 正文不进入普通日志

#### Scenario: 资源损坏
- **WHEN** 任一必需资源缺失、为空或不是有效 UTF-8
- **THEN** Control 在接受 Assist 请求前明确失败，不以旧内嵌正文静默降级

#### Scenario: 组装结果被调用者尝试修改
- **WHEN** 调用者尝试修改 Builder 返回的消息内容或诊断字段
- **THEN** 已构造的结果不变化，静态资源没有被当成动态模板执行

### Requirement: 请求级事实快照与信任分层
系统 SHALL 对本轮已验证的 Working Copy、基准版本、最近消息、授权 Secret 名称、Snippet、解析附件、图片和保存输入投影建立深层隔离的请求快照。当前 Working Copy MUST 是代码事实来源；当前 user 消息 SHALL 承载动态状态和参考材料，代码注释及外部材料 MUST NOT 获得 system 指令权。

#### Scenario: 原对象在组装后被修改
- **WHEN** 调用者在请求快照建立后修改嵌套 dict、列表或模型
- **THEN** 已组装的消息和诊断不变化，Credential 真值、原始正文和图片 base64 不出现在诊断中

#### Scenario: 历史或附件声称替换当前代码
- **WHEN** 历史 assistant 文本、代码注释、日志或附件与当前 Working Copy 冲突
- **THEN** 本轮代码提议仍依据当前 Working Copy，参考材料不能覆盖系统规则或代码事实

### Requirement: 按实际能力和材料组装消息
系统 SHALL 以固定顺序加载稳定规则、由代码生成的输出 Schema 与当前语言 Runtime Contract；只有实际向 Provider 提供工具时才加载工具规则，只有本轮明确启用且能力可用时才加入知识规则。Snippet、附件、图片和保存输入说明 SHALL 仅在各自材料有效存在时加入。历史 user/assistant 顺序、严格输出包装和合法工具消息配对 MUST 保留。

#### Scenario: 无工具的普通咨询
- **WHEN** 本轮没有提供工具且未启用知识检索、Snippet 或附件
- **THEN** Provider 请求不含 tools 字段及无关规则或空材料数组，普通解释可返回 `candidate:null`

#### Scenario: 显式知识检索
- **WHEN** 用户开启知识检索且已授权工具可用
- **THEN** 现有先列知识库、再搜索及有界纠正状态机继续生效，普通咨询规则不能跳过它

#### Scenario: 检索能力不可用
- **WHEN** 本轮显式要求知识检索但来源或能力不可用
- **THEN** 响应明确说明未完成检索或采用现有安全降级，不伪称检索成功

#### Scenario: 保存输入与图片
- **WHEN** 本轮存在 `saved_managed_input` 标签或经校验的图片
- **THEN** 保存输入只提供安全标签和对应语言读取指引；图片仅在 Provider 能力支持时作为当前 user 消息的多模态部分发送，不能声称已读取未作为附件提供的文件正文

### Requirement: 用户意图、严格结果与人工确认
系统 SHALL 维持 `AiModelOutput` 的严格解析和服务端校验。问候、帮助、解释及仅分析材料的请求 SHALL 以自然语言答复并令 Candidate 为 null；明确要求创建或修改代码时 Candidate SHALL 是完整代码快照。AI MUST NOT 自行保存、运行、发布、修改 Credential 或生命周期配置，Apply 仍只更新浏览器 Working Copy。

#### Scenario: 普通问题与显式修改
- **WHEN** 用户分别要求解释当前 Adapter 和直接修改 Adapter 代码
- **THEN** 前者不产生 Candidate，后者仅提出完整代码供人审核与 Apply；系统不自动保存或运行

#### Scenario: 无效或越权模型输出
- **WHEN** Provider 返回额外字段、截断内容、无效代码候选、Secret 反射或不同的 requirements/runtime_config
- **THEN** 现有服务端拒绝路径生效，不因 Prompt 迁移而放宽
