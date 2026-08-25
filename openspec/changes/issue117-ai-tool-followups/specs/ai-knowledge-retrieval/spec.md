## Purpose

让“知识库检索”开关成为服务端可验证的证据优先合同：启用后必须先取得知识库事实再回答，并在无结果或失败时清楚区分检索事实与模型补充。

## ADDED Requirements

### Requirement: 启用后强制知识库优先
当 `knowledge_search_enabled=true` 时，系统 MUST 在接受最终模型回答前执行受控知识库检索流程：先调用 `list_knowledge_bases`，在获得可用知识库后至少调用一次 `search_knowledge`，并在搜索返回相关且可读取的条目时调用 `read_knowledge` 获取正文。模型直接返回的未检索最终内容不得绕过此流程。

#### Scenario: Provider 首轮直接给出答案
- **WHEN** 已启用知识库检索但 Provider 在尚未调用 `list_knowledge_bases` 时直接返回最终内容
- **THEN** 系统 MUST 不接受该内容为最终回答，并继续要求执行 `list_knowledge_bases`

#### Scenario: 正常命中知识条目
- **WHEN** 列表返回可用知识库、搜索返回相关可读条目且读取成功
- **THEN** 系统 MUST 按 `list_knowledge_bases` → `search_knowledge` → `read_knowledge` 的顺序完成取证后才生成最终回答

#### Scenario: 列表为空
- **WHEN** `list_knowledge_bases` 成功但没有返回可用知识库
- **THEN** 系统 MUST 停止知识库工具链并在最终回答中明确说明没有可检索的知识库，不得发起无目标搜索

#### Scenario: 搜索无结果
- **WHEN** 已完成列表和搜索但没有匹配条目
- **THEN** 系统 MUST 不调用无目标的 `read_knowledge`，并在最终回答中明确说明知识库未检索到匹配内容

### Requirement: 检索失败透明说明
知识库配置不可用、列表失败、搜索失败或正文读取失败时，系统 MUST 在最终回答中说明失败阶段和可公开的稳定错误含义。若仍提供模型固有知识，系统 MUST 将其明确标记为模型补充，不得表述为知识库检索结论或伪造来源。

#### Scenario: 知识源调用失败
- **WHEN** 知识源在列表、搜索或读取阶段返回错误
- **THEN** 最终回答 MUST 指出知识库检索未完成及失败阶段，并将任何通用知识与检索结果清晰区分

#### Scenario: 已有部分检索结果后读取失败
- **WHEN** 列表和搜索成功但选中条目的正文读取失败
- **THEN** 系统 MUST 只使用已取得的脱敏搜索摘要，明确正文未读取成功，不得编造条目内容

### Requirement: 回答基于实际检索证据
知识库检索成功时，最终回答 SHALL 优先使用实际工具结果，并 MUST 只引用结果中真实存在的知识库、条目或来源标识。模型补充内容不得覆盖、扭曲或伪装为检索证据。

#### Scenario: 回答引用知识来源
- **WHEN** 最终回答提及某个知识库或知识条目作为依据
- **THEN** 该标识 MUST 能在本次 Assist 的脱敏工具结果中找到，且回答不得引用未返回的来源

#### Scenario: 检索结论与模型补充并存
- **WHEN** 模型在知识库证据之外增加通用背景知识
- **THEN** 回答 MUST 明确区分“知识库检索结果”和“模型补充”，使用户能够判断证据边界

### Requirement: 知识库流程避免无意义循环
知识库强制流程 MUST 复用工具预算、重复调用检测、连续失败保护和总时限。系统不得为了满足优先检索而重复执行等价的列表、搜索或读取调用，也不得在空列表、空搜索结果或不可恢复错误后继续无目标调用。

#### Scenario: Provider 重复请求同一搜索
- **WHEN** Provider 在没有新知识库结果的情况下重复请求相同知识库和相同查询
- **THEN** 系统 MUST 拦截重复搜索并转入透明的最终回答，不得再次访问知识源

#### Scenario: 保护边界在知识库流程中触发
- **WHEN** 知识库工具链达到连续失败、预算或总时限边界
- **THEN** 系统 MUST 停止继续检索，保留已取得证据，并按失败或不完整状态生成最终回答

### Requirement: 未启用时保持兼容
当 `knowledge_search_enabled=false` 时，系统 SHALL 保持当前可选只读工具策略，不得强制执行知识库列表、搜索或读取，也不得因本 capability 改变现有 Candidate 和 Assist 请求响应合同。

#### Scenario: 用户未勾选知识库检索
- **WHEN** Assist 请求的 `knowledge_search_enabled` 为 `false`
- **THEN** 系统 MUST 不强制调用任何知识库工具，并按现有非知识库流程生成回答
