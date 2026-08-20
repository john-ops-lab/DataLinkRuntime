# M4 AI Editor Spec

> **当前现行性：Partially current。**  
> Working Copy、Candidate、Secret、Context Snippets、Provider 安全边界、stale/Diff/Apply 与 hidden reasoning 隔离等核心合同仍是当前 AI Assistant 的基础；但本文中的旧 `Publish / Production / Start` 生命周期文字已被 M5.4+ 替代，M4 的“禁止 Tool Call / RAG”范围也将由 M5.7 的受控只读 Tool Call、Attachments 与 MCP 合同扩展。  
> **M5.7 开发不得把本文当作完整现行权威。** 当前目标以 [Issue #80](https://github.com/john-ops-lab/DataLinkRuntime/issues/80) 与 `docs/specs/m5-7-ai-assistant.md` 为准；文档优先级见 `docs/specs/README.md`。

> M4 原始上位契约：[GitHub Issue #14](https://github.com/john-ops-lab/DataLinkRuntime/issues/14)。
> 本文记录 M4 当时的实际接口、状态边界与验证口径；未被后续里程碑替代的部分继续有效。

## 1. 范围与成功路径

M4 在既有三语言 Adapter Workbench 右侧增加 Human-in-the-loop AI Assistant：

```text
当前 Working Copy + 最小上下文
→ AI 返回解释 + 完整 Candidate Snapshot
→ 管理员查看 Diff
→ 管理员明确 Apply
→ 仅浏览器 Working Copy 变 dirty
→ 管理员后续自行 Save / Test / Publish / Start
```

AI Assist 不是生命周期入口。服务端处理一次 Assist 时不得创建 AdapterVersion、Execution，
不得移动 `latest_version_id / published_version_id`，不得改变 `production_state` 或生产 Worker，
也不得修改 Credential、绑定或 Adapter.language。

## 2. 持久化模型与 Credential

M4 只维护一条全局活动 AI 模型设置：

| 字段 | 合同 |
|---|---|
| provider | `openai / deepseek / kimi / minimax / custom_openai_compatible` |
| base_url | 管理员明确配置的模型服务根 URL |
| model | 管理员明确选择或手工输入的 Model ID；不自动升级 |
| credential_id | 可空；非空时必须引用 `token` Credential |
| reasoning_mode | `default / enabled / disabled`，默认 `default` |
| reasoning_effort | 可空；Provider 支持集的并集为 `low / medium / high / xhigh / max` |
| created_at / updated_at | 设置时间戳 |

API Key 不进入设置表。Control 调用 Provider 时才在内存中解密所引用 Credential 的
`token` 字段并用作 Bearer Token。设置响应只返回 Credential 引用与名称等元数据。

以下内容不持久化：模型刷新列表、对话、Prompt、Working Copy、Provider Response、
Candidate、reasoning。正式开发历史仍只有管理员 Save 后产生的不可变 AdapterVersion。

## 3. 管理 API

所有接口沿用管理员 Bearer Token 认证。

### 3.1 AI 设置

```text
GET  /api/ai/settings
PUT  /api/ai/settings
POST /api/ai/settings/test
POST /api/ai/models/refresh
```

- `GET` 返回当前 `AiModelSetting`；尚未保存时返回 HTTP 200 JSON `null`。
- `PUT` 接收 §2 的可写字段并执行 singleton upsert，返回同字段加
  `id=1 / credential_name / created_at / updated_at`，且不含 Secret。
- `settings/test` 接收同一份待测设置，向对应 Provider 发出真实但最小的 completion
  请求；只有 Provider 请求格式和 final answer 均可解析时才返回
  `{ "ok": true, "message": "Connection successful" }`。
- `models/refresh` 接收 `provider / base_url / credential_id`，调用 `/v1/models` 并返回
  `{ "models": ["model-id", ...] }`。刷新失败不影响管理员手工输入并保存 Model ID。

设置保存与连接测试彼此独立：测试未保存的表单不应暗中替换全局活动配置；刷新模型也不
自动改变已保存 Model ID。

### 3.2 Adapter AI Assist

```text
POST /api/adapters/{adapter_id}/ai/assist
```

请求：

```json
{
  "message": "增加分页处理",
  "working_copy": {
    "code": "...当前完整代码...",
    "requirements": "...当前完整依赖声明...",
    "runtime_config": {}
  },
  "recent_messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "base_version_id": 12,
  "context_snippets": [
    {
      "source": "code",
      "text": "...管理员在 Monaco 中实际选中的精确文本...",
      "start_line": 12,
      "end_line": 28
    },
    {
      "source": "log",
      "text": "...浏览器可见的已脱敏实时日志选中文本...",
      "start_line": 1,
      "end_line": 4
    }
  ],
  "attachments": [
    {
      "filename": "requirements.pdf",
      "content_type": "application/pdf",
      "data_base64": "...严格 base64..."
    }
  ]
}
```

`attachments`（M5.7 Wave B2）可空，最多 8 个，仅服务本次请求（不落库、不写临时
文件、不写日志）。完整合同（上限、能力表、错误码、解析与隐私边界）见
`docs/specs/m5-7-ai-assistant.md` §7.5；能力表另见
`GET /api/ai/attachment-capabilities`。不带该字段的请求与 Wave A 之前完全兼容。

`recent_messages` 最多 8 条，只允许 `user / assistant` 可见消息；旧 Candidate、旧代码快照
和 reasoning 不进入历史。`base_version_id` 可空；非空时只用于补充属于当前 Adapter 的
基准 Version 元数据，不把客户端字段当作版本事实来源。

`context_snippets`（M5.5.13）可空，最多 20 条，按管理员加入顺序排列；每条是管理员点击
「加入对话上下文」瞬间捕获的**精确快照**：

- `source` 为 `"code"`（Monaco 代码选区）或 `"log"`（实时日志选区）；
- `text` 必须是本次实际选中的非空文本，且**原样保留**（前导缩进与行尾换行不被裁剪）；
  日志片段的 `text` 只能来自浏览器已经过脱敏处理的可见文本（如 `[REDACTED]`），
  Control 不读取原始日志、不解密任何 Secret 真值；
- `start_line / end_line` 是 1-based 行号且 `1 ≤ start_line ≤ end_line`；
- 单条 `text` 上限 50,000 字符。

浏览器必须在点击瞬间冻结快照，管理员随后移动光标不得改变已发送的片段；空选区、纯空白
文本、非法行号、未知 `source` 或超出条数/长度上限返回 HTTP 422 `ai_request_invalid`
且不回显非法原值。片段只作为结构化文本块进入本轮 Prompt，不包含文件路径、不触发任何
文件读取，也不持久化；片段不跨 Adapter 串线，Adapter 切换时浏览器立即清空。

Control 必须根据 `adapter_id` 自行读取并补充：

- Adapter id 与不可变 language；
- 对应语言 Runtime Contract；
- 基准 Version 信息（如有）；
- 当前 Credential bindings 的 `env_key` 名称；
- `context.config / context.secrets.get(key) / context.logger` 与 JSON I/O 语义。

Control 不读取或发送绑定 Credential 的真值。浏览器提交的 Working Copy 是本轮唯一权威
代码快照，但无权借请求修改 Adapter.language 或任何生命周期字段；上下文片段同样只影响
AI 生成建议，不改变 Working Copy 基线、Candidate schema、stale 判定或 Diff / Apply 语义。

响应：

```json
{
  "message": "已生成修改候选",
  "candidate": {
    "summary": "增加 next_token 分页处理",
    "code": "...完整候选代码...",
    "required_secret_keys": ["API_TOKEN"]
  },
  "provider": "custom_openai_compatible",
  "model": "model-id"
}
```

`candidate` 可为 `null`，表示只解释、不建议修改。当前实现始终返回 `provider / model`
作为非敏感路由元数据。

## 4. Candidate 与 Provider 适配

### 4.1 Candidate Schema

M5.8-003 起，非空 Candidate 的 AI 可修改内容只有 `code`。必须满足：

- `summary`：string；
- `code`：non-empty string；
- `required_secret_keys`：string[]。

Candidate 是完整代码快照，不是 patch；不包含 requirements、runtime_config、Credential
Binding、Worker / Schedule / Webhook 等人工运行配置，也不包含 language、Credential 真值、
production Worker、Version / Execution 指针或 production state。旧 Provider 若仍返回
`requirements` / `runtime_config`，二者仅作为可选兼容回显，必须与本轮 Working Copy 完全
一致；任意差异均按 Provider 合同违规拒绝，前端 Diff / Apply 永不展示或应用它们。无论
Provider 是否声明 Structured Output，Control 都必须执行本地 Schema Validation；禁止用正则
截代码、猜 JSON 或模糊应用 patch。

### 4.2 OpenAI-compatible 薄适配

五种 Provider 共用 `GET /v1/models` 与 `POST /v1/chat/completions` 主协议；base_url 可为
服务根 URL 或以 `/v1` 结尾，Control 只追加一次 `/v1`。实际薄适配如下：

| Provider | Structured output | 显式 reasoning 映射 |
|---|---|---|
| openai | `json_object` | `enabled` 必须显式选择 `low / medium / high / xhigh` 并映射为 `reasoning_effort`；不支持 `max`，且通用 Chat Completions 无可靠 disabled，故拒绝 |
| deepseek | `json_object` | `thinking.type=enabled/disabled`；enabled 时可显式发送 `reasoning_effort=high/max` |
| kimi | `json_object` | `thinking.type=enabled/disabled`；不映射 effort |
| minimax | prompt-only JSON | 始终加 `reasoning_split=true` 仅用于 final/reasoning 分离；显式开关/effort 拒绝 |
| custom_openai_compatible | prompt-only JSON | 显式开关/effort 拒绝 |

M4 不维护动态插件、模型版本大清单、模型路由、负载均衡或 fallback。

OpenAI 不使用 strict `json_schema`：历史兼容回显中的 Candidate `runtime_config` 可能是任意
JSON object，而 OpenAI strict schema 要求所有 object 都是 closed object；强行生成该
schema 会错误收窄兼容字段合同。因此 OpenAI 使用 JSON mode，最终结果仍统一经过 DLR
本地 `AiModelOutput` 严格校验。

`reasoning_mode=default` 时不发送 reasoning override。OpenAI 选择 `enabled` 但未指定 effort
也返回 `ai_reasoning_unsupported`，不得由 DLR 擅自补 `medium`。管理员显式选择的 reasoning
配置若当前 Provider 不支持，返回 `ai_reasoning_unsupported`，不得静默忽略。Web 根据上述
Provider 级能力动态展示 effort，不维护脆弱的 model allowlist。

Provider HTTP 使用统一、有界的部署参数 `DLR_AI_PROVIDER_TIMEOUT_SECONDS`，默认 180 秒，
允许 10～600 秒。该 deadline 适用于 models / connection test / assist；M4 仍保持非流式，
也不新增输出 token 参数 UI、后台任务或队列。

Provider Adapter 只向上层交付 `final_text`：

- `reasoning_content / reasoning_details` 丢弃；
- 只剥离 final text 开头一个或多个明确闭合的 `<think>...</think>` 容器；进入最终 JSON
  文档后不再扫描 Candidate 内部字符串，代码或配置中的合法 `<think>` 字面量允许保留；
- 只有明确 `finish_reason=stop` 的完整回答才接受，截断或过滤的回答整次拒绝；
- reasoning 不返回浏览器、不保存、不记录、不进入下一轮；
- final answer 无法可靠分离时返回 `ai_response_invalid`。

## 5. Web 交互与防覆盖

- AI 面板位于 Workbench 右侧。默认收起为右侧悬浮入口（绝对定位、不占用布局，
  不压缩 Monaco 主编辑区），点击展开为 360–420px 对话面板；收起/展开均可键盘
  操作，展开时面板回到布局流中，不遮挡保存/运行按钮。
- 聊天卡片中 Candidate 只提供「查看修改」单一路径；Apply 只出现在 Diff 内
  （「应用修改」/「关闭」），关闭 Diff 即不应用，无额外「放弃」动作。
- 未选择 Adapter 或 Working Copy 尚未就绪时不可发送。
- 对话与 Candidate 绑定当前 Adapter；切换 Adapter 时清空会话。
- 请求使用 generation + adapter id 防护；旧响应不得写入新 Adapter。
- 发出请求时保存 code / requirements / runtime_config 的 base snapshot。响应回来时若当前
  Working Copy 的 code 与 base 不同，Candidate 标为 stale；requirements / runtime_config
  的人工修改不影响代码 Candidate 的 stale 判定。
- Diff 按 Adapter.language 使用正确 Monaco language，只展示 code；required_secret_keys
  单独展示，不自动创建绑定。
- 缺少建议 Secret binding 时显示警告，但不阻止管理员审阅 Candidate。
- 已归档 Adapter 只读，不允许 Apply。
- Apply 只替换浏览器 Working Copy 的 code 并进入既有 dirty 状态，不调用 Save / Test /
  Publish API；requirements、runtime_config 与 Credential Binding 始终保持人工编辑值。

### 5.1 上下文片段（M5.5.13）

- 编辑工具栏提供「加入对话上下文」按钮，仅在当前存在**非空选区**且 Working Copy
  就绪时可用；空选区/纯空白文本不产生任何操作。点击后 AI 面板**自动展开**。
- 点击瞬间从 Monaco 读取本次实际选择的精确文本与 1-based 行号作为快照；AI 面板展示
  「代码 第 12–28 行」标记（语言展示名来自稳定映射，不伪造文件路径）。
- 实时日志 Tab 的「统一日志」同样提供「加入对话上下文」：选中可见日志文本（普通日志 /
  stderr / 错误日志 / Traceback 均在同一统一视图）后点击，只读取浏览器已渲染的
  **已脱敏可见文本**（如 `[REDACTED]`）与选中行的行号；不读取原始日志、不绕过脱敏。
  面板展示「实时日志 10:21:03–10:21:08」时间范围标记（由选中首尾行的统一时间前缀推导，
  无时间前缀时退回行号范围）。
- 同一会话支持**多个上下文片段**（代码 + 代码、代码 + 日志、日志 + 日志），新片段追加
  不覆盖旧片段；每片段独立展示来源与范围，可单独删除某一片段，也可一键清空全部。
- 发送请求时按加入顺序携带全部片段快照；后续光标移动不会悄悄改变请求内容。
- 片段属于当前 Adapter / 当前会话：切换 Adapter 时标记与快照立即清理，旧片段绝不串到
  新 Adapter；片段不持久化到数据库、localStorage 或普通日志。
- 片段不引入 Secret 真值：日志片段只携带浏览器可见脱敏文本，Control 仍只向 Prompt
  注入绑定 `env_key` 名称；片段只影响 AI 生成建议，不改变 stale 判定、Candidate schema、
  Diff/Apply 与 Save/Test/Run 人工门禁。
- 引导文案不暴露「工作副本 / 唯一代码快照」等内部术语，明确引导使用「凭据绑定」：
  绑定的 Secret 不进入 AI Prompt，但硬编码在代码中的敏感信息会随代码上下文发送。

### 5.2 平台阶段进度（M5.5.5）

请求进行中，AI 面板展示 DLR 自身已知的请求生命周期阶段，而非模型推理：

```text
正在准备当前代码上下文… → 正在请求 AI 模型… → 正在校验返回结果…
→ 已生成修改，等待查看 Diff（成功收敛）
```

- 进度只由浏览器侧请求生命周期驱动：组装请求、网络请求、结果校验、提交完成；
  不展示 token-by-token chain-of-thought，不在 Prompt 中要求 Provider 输出思考过程，
  不解析 Provider 私有 reasoning 字段。
- 成功收敛态「已生成修改，等待查看 Diff」只在确实生成 Candidate 时展示；
  Provider 只返回纯文本说明（candidate=null）时进度静默收敛到回复本身，不误导。
- 请求失败时进度立即收敛到明确错误状态（错误提示出现、进度行消失）。
- 请求取消 / Adapter 切换后旧进度不得继续覆盖新会话（generation + Adapter key 双重隔离）。

## 6. 稳定错误与日志边界

M4 对外提供以下稳定错误码：

```text
ai_not_configured
ai_credential_invalid
ai_provider_dns_failed
ai_provider_unreachable
ai_auth_failed
ai_model_not_found
ai_timeout
ai_reasoning_unsupported
ai_response_invalid
ai_request_invalid
ai_base_url_invalid
ai_working_copy_invalid
```

`ai_provider_dns_failed`（M5.5.3）仅表示模型服务域名解析失败（DNS 层），与表示 TCP 连接 /
TLS 握手失败的通用传输错误 `ai_provider_unreachable` 区分，便于部署排障按层级定位。

其中非法 Unicode 等通用 AI 请求边界错误返回 HTTP 422 `ai_request_invalid`；无法安全解析的
Base URL 返回 HTTP 422 `ai_base_url_invalid`；Working Copy 的 runtime_config 含非有限或
非 JSON 值时返回 HTTP 422 `ai_working_copy_invalid`。这些输入错误响应不回显非法原值。

Provider 原始错误不得把 Authorization、请求体、Prompt、Working Copy 或 Provider Response
透传给浏览器或普通日志。Provider HTTP 调用不跟随重定向，避免把 Bearer Token 或请求体
带到未配置的地址；若模型列表或 final answer 回显完整 Provider Token，则整次响应按
`ai_response_invalid` 拒绝。严格 JSON 解析也拒绝非有限数字、重复对象键、截断及无法稳定
解析的深层/超大值。允许的运行元数据仅限 provider、model、耗时与成功/失败等不敏感字段。

## 7. 验证矩阵

| 层 | 必须证明 |
|---|---|
| Backend | 设置 CRUD 不回显 Secret；token Credential/null 校验；五类 Provider fixture 归一；reasoning 隔离；default 不发 override；unsupported 稳定报错；models 归一；Prompt 只含 env_key；三语言 Contract；Candidate 严格校验；Assist 生命周期零副作用；代码/日志多片段按序入 Prompt 且 Secret 真值不入 Prompt；日志片段仅承载脱敏可见文本；非法片段（空白/行号越界/倒序/未知 source/超条数/超长度/代理项）稳定 422 不回显；片段不落库不落日志 |
| Web | Panel 收放；发送当前 Working Copy；三语言 Diff；Apply 成功后自动关 Diff 且零生命周期 API，失败/锁定/校验拒绝时保留 Diff；Apply dirty 且零生命周期 API；缺绑定提示；stale 明确覆盖；Adapter 切换丢弃旧响应并清空会话；Archived 禁 Apply；Model 刷新 + 手输；reasoning 默认值；非空选区一键加入并自动展开面板与行号标记；实时日志选区（已脱敏可见文本）加入并显示时间范围；多片段追加/单删/清空；Adapter 切换隔离旧片段；发送使用快照不受光标影响；悬浮入口可拖动且不误触点击、刷新恢复默认位置、不持久化坐标；四阶段平台进度与失败收敛；旧进度不覆盖新会话；顶部主蓝色与新引导文案（凭据绑定引导 + 硬编码敏感信息可能随代码发送的区分） |
| Compose smoke | 在隔离 Compose 网络启动临时本地 fake Provider，完成 settings→`/v1/models`→真实最小 `/v1/chat/completions`→Python/JavaScript/Java Assist，并逐个断言 Version / Execution 数量、published pointer、production state 不变；代码 + 脱敏日志多片段快照随请求到达 fake Provider，reasoning 哨兵与片段文本不出现在响应与服务日志；不访问公网 AI，fake 不进入正式 Compose |

标准门禁：backend ruff / format / mypy / pytest，web lint / typecheck / tests / build，以及
`./scripts/compose-smoke.sh` 全绿。构建成功不能替代上述真实行为验证。

## 8. 明确不做

AI 自动 Save / Test / Publish / Start / Stop / Unpublish / Archive / Restore；自动调试循环；
tool calling 执行 DLR 动作；对话持久化；RAG / Embedding / Vector DB；LangChain / LlamaIndex /
Agent Framework；多模型路由 / fallback；Provider streaming；reasoning 展示或存储；任意认证
Header / Basic Auth / OAuth；Schedule / Webhook / RBAC；Draft 表、Edit Lock 或协作框架。
