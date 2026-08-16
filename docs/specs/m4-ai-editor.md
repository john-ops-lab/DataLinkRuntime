# M4 AI Editor Spec

> 上位契约：[GitHub Issue #14](https://github.com/john-ops-lab/DataLinkRuntime/issues/14)。
> 本文只记录 M4 的实际接口、状态边界与验证口径；未覆盖处以 Issue #14 为准。

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
  "base_version_id": 12
}
```

`recent_messages` 最多 8 条，只允许 `user / assistant` 可见消息；旧 Candidate、旧代码快照
和 reasoning 不进入历史。`base_version_id` 可空；非空时只用于补充属于当前 Adapter 的
基准 Version 元数据，不把客户端字段当作版本事实来源。

Control 必须根据 `adapter_id` 自行读取并补充：

- Adapter id 与不可变 language；
- 对应语言 Runtime Contract；
- 基准 Version 信息（如有）；
- 当前 Credential bindings 的 `env_key` 名称；
- `context.config / context.secrets.get(key) / context.logger` 与 JSON I/O 语义。

Control 不读取或发送绑定 Credential 的真值。浏览器提交的 Working Copy 是本轮唯一权威
代码快照，但无权借请求修改 Adapter.language 或任何生命周期字段。

响应：

```json
{
  "message": "已生成修改候选",
  "candidate": {
    "summary": "增加 next_token 分页处理",
    "code": "...完整候选代码...",
    "requirements": "...完整候选依赖声明...",
    "runtime_config": {},
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

非空 Candidate 必须同时满足：

- `summary`：string；
- `code`：non-empty string；
- `requirements`：string；
- `runtime_config`：JSON object；
- `required_secret_keys`：string[]。

Candidate 是完整快照，不是 patch；不包含 language、Credential 真值、production Worker、
Version / Execution 指针或 production state。无论 Provider 是否声明 Structured Output，
Control 都必须执行本地 Schema Validation；禁止用正则截代码、猜 JSON 或模糊应用 patch。

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

OpenAI 不使用 strict `json_schema`：Candidate 的 `runtime_config` 按产品合同必须允许任意
JSON object，而 OpenAI strict schema 要求所有 object 都是 closed object；强行生成该
schema 会错误收窄运行参数合同。因此 OpenAI 使用 JSON mode，最终结果仍统一经过 DLR
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
  Working Copy 与 base 不同，Candidate 标为 stale；管理员仍可查看相对当前内容的 Diff，
  但必须明确点击“仍然应用”。
- Diff 按 Adapter.language 使用正确 Monaco language，并覆盖 code、requirements 与
  runtime_config；required_secret_keys 单独展示，不自动创建绑定。
- 缺少建议 Secret binding 时显示警告，但不阻止管理员审阅 Candidate。
- 已归档 Adapter 只读，不允许 Apply。
- Apply 只替换浏览器 snapshot 并进入既有 dirty 状态，不调用 Save / Test / Publish API。

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
| Backend | 设置 CRUD 不回显 Secret；token Credential/null 校验；五类 Provider fixture 归一；reasoning 隔离；default 不发 override；unsupported 稳定报错；models 归一；Prompt 只含 env_key；三语言 Contract；Candidate 严格校验；Assist 生命周期零副作用 |
| Web | Panel 收放；发送当前 Working Copy；三语言 Diff；Apply dirty 且零生命周期 API；缺绑定提示；stale 明确覆盖；Adapter 切换丢弃旧响应并清空会话；Archived 禁 Apply；Model 刷新 + 手输；reasoning 默认值 |
| Compose smoke | 在隔离 Compose 网络启动临时本地 fake Provider，完成 settings→`/v1/models`→真实最小 `/v1/chat/completions`→Python/JavaScript/Java Assist，并逐个断言 Version / Execution 数量、published pointer、production state 不变；不访问公网 AI，fake 不进入正式 Compose |

标准门禁：backend ruff / format / mypy / pytest，web lint / typecheck / tests / build，以及
`./scripts/compose-smoke.sh` 全绿。构建成功不能替代上述真实行为验证。

## 8. 明确不做

AI 自动 Save / Test / Publish / Start / Stop / Unpublish / Archive / Restore；自动调试循环；
tool calling 执行 DLR 动作；对话持久化；RAG / Embedding / Vector DB；LangChain / LlamaIndex /
Agent Framework；多模型路由 / fallback；Provider streaming；reasoning 展示或存储；任意认证
Header / Basic Auth / OAuth；Schedule / Webhook / RBAC；Draft 表、Edit Lock 或协作框架。
