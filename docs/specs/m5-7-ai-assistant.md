# M5.7 AI Assistant UI 组件化与知识接入扩展——当前规格

> 状态：Current target / 待实现  
> 父 Issue：[#20 M5](https://github.com/john-ops-lab/DataLinkRuntime/issues/20)  
> 当前阶段合同：[#80 M5.7](https://github.com/john-ops-lab/DataLinkRuntime/issues/80)  
> 前置：M5.6 / #70 已完成并通过用户中英文人工验收。  
> 冲突处理：Issue #80 > 最新 `main` 现状 > 当前 product/architecture > 本文 > 历史 specs。

本文用于把 M5.7 的稳定技术边界集中记录在仓库中，避免继续从 M1～M4 的历史产品语义推导当前实现。Issue #80 仍是本阶段完整产品范围和验收口径的最高权威。

## 1. 目标

M5.7 在**不把 DLR 1.x 升级为通用 Agent Runtime**的前提下：

```text
现有 AI Assistant
→ assistant-ui 组件化 Chat UI
→ Regenerate
→ Attachments
→ 受控只读 Tool Call
→ MCP / 接口知识库
```

核心 Human-in-the-loop 路径保持：

```text
当前 Working Copy + 本轮上下文
→ AI 回答 / 受控知识检索
→ 完整 Candidate Snapshot（如需要修改）
→ 管理员查看 Diff
→ 管理员明确 Apply
→ 只修改浏览器 Working Copy
→ Save / Test / Run 仍由管理员自行触发
```

## 2. 明确不做

M5.7 不实现：

```text
Streaming / SSE token-by-token 输出
Reasoning UI / Chain-of-Thought 展示
通用 Agent Runtime
AI 自动 Save / Test / Run
AI 自动 Worker / Schedule / Webhook 生命周期操作
无限制 Shell / 文件系统 / SQL / HTTP Tool
MCP 写操作
Thread 持久化
多会话历史列表
Edit Message
Branch
Speech
assistant-ui Cloud 依赖
永久企业级向量数据库
M6 Production Hardening
```

hidden reasoning 继续在 Provider 边界丢弃，不返回浏览器、不持久化、不进入下一轮。

## 3. assistant-ui 融合边界

前端保持现有技术栈：

```text
React 19 + TypeScript + Vite + Ant Design + Monaco
```

引入 `@assistant-ui/react` 时优先采用 headless primitives / External Store Runtime（或当前版本的等价外部 Store 适配方式），不得为了聊天侧栏迁移到 Next.js、Tailwind 或 shadcn/ui。

### assistant-ui 负责

- Thread；
- User / Assistant Message 基础结构；
- Composer；
- Running / Loading；
- 自动滚动与 Scroll-to-bottom；
- Markdown / GFM；
- Code Block；
- Copy；
- Regenerate 入口；
- Attachment UI；
- Tool Call UI。

### DLR 继续负责

- AI 悬浮入口、拖动、展开/收起；
- 当前 Adapter / Version 上下文；
- Context Snippets；
- Working Copy；
- Candidate Schema 与 Candidate Card；
- Secret Binding 检查；
- stale 判定；
- Diff / Apply；
- Adapter switch / late response 隔离；
- Provider / Model / Credential；
- i18n 与稳定错误合同。

第三方 runtime 不得成为 Candidate、Working Copy 或安全状态的唯一事实来源。

## 4. 必须继承的 AI 核心合同

M5.7 继续保持：

- 当前 Working Copy 是本轮请求唯一权威代码快照；
- `recent_messages` 只含浏览器可见 `user / assistant` 消息并保持有界，当前上限 8 条；
- Candidate 是完整 Snapshot，不是 patch；
- Candidate 必须经过 DLR 本地严格 Schema Validation；
- AI 不得修改 `language / adapter_type / runtime_worker_id` 或生命周期字段；
- Apply 只替换浏览器 Working Copy，不自动 Save / Test / Run；
- Secret 真值不得进入 Prompt、浏览器、附件状态、Tool 参数/结果展示或普通日志；
- Context Snippets 仍是管理员显式冻结的代码/脱敏日志片段；
- Adapter switch 和 generation guard 继续阻止旧响应串入新 Adapter。

## 5. Prompt / Provider 的最小扩展

M4 System Prompt 当前存在“不得 tool call”的硬限制；M5.7 正式加入只读 Tool Call，因此允许做**最小必要修改**：

```text
模型只可调用 DLR 显式注册的 read-only tools
→ DLR 执行有界工具调用
→ 将安全结果返回模型
→ 最终仍必须生成符合 AiModelOutput 的 final JSON
```

不得借此重写整套 Prompt、引入开放式自主循环或允许模型自行发现/执行任意工具。

Provider 适配需要明确区分：

- 普通文本/Structured Output；
- 原生图片输入能力；
- 原生文件输入能力；
- Tool Call 能力。

“多模态模型”不等于自动支持 PDF / Word / 任意文件，能力必须显式判断或安全降级。

## 6. Regenerate

Regenerate 表示对某一轮 AI 回复重新生成，而不是重新读取当前编辑器状态形成新问题。

每轮发送时应冻结最小可重放快照，包括：

- user message；
- Working Copy；
- base version；
- 当时的 recent_messages；
- context snippets；
- 本轮 attachments。

Regenerate 使用这份冻结快照重新请求；用户中途修改 Working Copy 不得偷偷改变原问题。

Regenerate：

- 不自动 Apply；
- 不建立 Branch / 多答案树；
- Adapter switch 后旧轮次不可跨 Adapter 使用；
- 继续使用 generation / adapter id 防晚到响应串线。

## 7. Attachments

### 7.1 首批支持类型

上传入口必须明确展示支持范围、大小和能力限制。

```text
图片：PNG / JPEG / WebP
文档：PDF / DOCX（legacy DOC 只有在实现成本和解析可靠性可接受时才支持，否则明确提示不支持）
文本：TXT / MD / CSV / JSON / YAML / YML
代码：常见源码和配置文本文件
```

实际可用性受当前 Provider / Model 能力影响。

每个附件至少有清晰状态：

```text
已就绪
使用模型原生文件能力
DLR 提取文本
文档较大，将按相关内容检索
当前模型不支持图片
类型不支持
解析失败
超过大小限制
```

### 7.2 处理策略

```text
上传
→ 前后端校验类型 / 文件数 / 单文件大小 / 总大小
→ 检查 Provider + Model 能力
   ├─ 原生支持：优先走 Provider 原生图片 / 文件输入
   └─ 不支持：进入 DLR fallback
```

Fallback：

- PDF / DOCX / 文本：服务端安全提取文本；
- 图片：模型不支持 Vision 时明确提示更换模型；**不偷偷 OCR 并伪装为视觉理解**；
- 解析库需适合无 GUI 容器环境；
- 解析正文不得写入普通日志或错误回显。

### 7.3 大文档

特别大的文档不得全文无脑进入 Prompt。

```text
小文档
→ 完整文本或 Provider 原生文件

大文档
→ 切块
→ 根据本轮问题做有界相关片段选择 / 临时检索
→ 只把相关内容送入模型
```

M5.7 只要求轻量、临时的文档检索，不建设永久向量库。

### 7.4 生命周期与隐私

- 附件默认只服务当前浏览器 AI 会话/请求；
- 不写 AdapterVersion / Revision；
- 不自动写入 ima 或其他知识库；
- Adapter switch 不得串线；
- Regenerate 可复用原轮冻结附件；
- UI 必须提示附件可能发送给管理员配置的第三方模型 Provider；
- DLR 不承诺能自动识别附件里的全部 Secret，因此不能把“自动脱敏附件”作为安全保证。

### 7.5 B2 服务端附件合同（已实现）

Wave B2 交付后端/API/types/tests 的完整附件服务端合同，供 Wave B3 直接消费；
B2 不做任何前端 UI（assistant-ui 面板保持 Wave A 原样）。

**传输与向后兼容**：附件仍走既有 JSON `POST /api/adapters/{id}/ai/assist`，
字段为 `attachments: [{filename, content_type, data_base64}]`（严格 base64）。
不携带或传空数组的请求与 Wave A 之前逐字节兼容（Prompt 不含附件指令）。

**能力表（显式，绝不假设）**：`GET /api/ai/attachment-capabilities` 返回
`limits`（数量/单文件/总量/解析文本预算/解析超时）、`supported_content_types`
与逐 Provider 的 `images_native` / `files_native`。当前：仅 `openai`
`images_native=true`；`deepseek / kimi / minimax / custom_openai_compatible`
均为 false（“多模态”不等于支持原生图片/文件）。

**处理策略**：

```text
图片 + 能力表支持  → OpenAI 风格原生 image_url content part（data URL）
图片 + 不支持      → 422 ai_attachment_image_unsupported（可行动，绝不 OCR）
PDF / DOCX / 文本/代码 → 服务端有界解析文本（pypdf / stdlib zip+XML）
无文本层（扫描件） → 422 ai_attachment_no_text（提示换模型，可行动）
```

**上限（前后端同源）**：单文件 6 MiB、总量 12 MiB、最多 8 个、单文件解析
文本 64 KiB 字符、总解析 256 KiB 字符（按附件数均分预算，`truncated` 标记）、
解析超时 30s（线程 + 硬截止）。错误码均为稳定 `detail.code` 且不回显文件
内容/文件名/base64：`ai_attachment_invalid`、`ai_attachment_filename_invalid`
（拒绝路径/遍历/控制字符）、`ai_attachment_type_unsupported`（未知 MIME、
MIME/扩展名不一致、magic bytes 不符）、`ai_attachment_too_large`、
`ai_attachment_total_too_large`、`ai_attachment_count_exceeded`、
`ai_attachment_parse_failed`、`ai_attachment_unsafe_archive`（DOCX zip 成员/
总量/膨胀比上限）、`ai_attachment_parse_timeout`(504)、`ai_attachment_no_text`。

**生命周期与隐私**：全程内存处理，不写临时文件、不落数据库/Thread/日志；
成功、校验失败、解析失败、超时均无残留。附件正文只进入本轮 Provider 请求；
响应、错误、日志一律不回显。Provider 对原生图片返回 400/422 时映射为
`ai_attachment_image_unsupported`，不泄露 Provider 错误体。

**依赖**：新增 `pypdf>=6,<7`（uv.lock 锁定）。理由：纯 Python（无 C 依赖，
适合无 GUI 容器）、BSD-3-Clause、活跃维护、无传递运行时依赖、wheel 约 0.4MB；
是能真实提取 PDF 文本的最小解析器。DOCX 只用 stdlib `zipfile` + `xml.etree`
（w:t 流式提取），不引入 lxml 等编译依赖。

**Wave B3 消费点**：能力端点驱动上传入口展示；`attachments` 字段驱动
AttachmentAdapter；错误码驱动附件状态文案；`truncated` 与解析预算驱动
“文档较大，将按相关内容检索”的降级提示（B3 再实现临时切块检索）。

## 8. Tool Call

M5.7 实现真实、受控、只读的 Tool Call，而不是只画 UI。

首批允许：

```text
知识库列表
知识库搜索
知识内容读取
DLR Runtime Contract / 平台帮助文档查询
```

禁止：

```text
Shell
任意文件系统读写
任意 SQL
任意外部 HTTP Tool
Save / Apply / Run / Stop
Worker 管理
Schedule / Webhook 生命周期修改
Credential 真值读取
```

单次 Assist 的：

- Tool 调用次数；
- 单次超时；
- 累计超时；
- 参数大小；
- 结果大小；

都必须有界，禁止形成无限循环。

Tool Call UI 至少展示：

- 工具名称；
- 调用中 / 成功 / 失败；
- 经过脱敏的必要参数摘要；
- 有界结果摘要。

浏览器不得看到 Secret、Credential 真值、超大原始 payload 或 hidden reasoning。

## 9. MCP 与腾讯 ima POC

M5.7 建立统一的只读知识工具边界，首个真实 POC 为腾讯 ima 知识库。

目标流程：

```text
用户：根据 XX 系统接口文档生成 Adapter
→ AI 调用知识库搜索
→ 获取 URL / Method / 参数 / 返回结构 / 示例
→ 生成 Candidate
→ 用户 Diff → Apply
```

第一版统一能力：

```text
list_knowledge_bases
search_knowledge
read_knowledge
```

腾讯 ima 接入规则：

- 优先使用官方开放接口；
- 如果账户/产品存在可直接消费的标准 MCP Endpoint，使用标准 MCP Client；
- 若没有，则允许实现薄 ima adapter，把官方 OpenAPI 映射到 DLR 统一只读 Tool/MCP 边界；
- 不依赖 WorkBuddy 作为 DLR Runtime；
- Client ID / API Key 等必须进入 DLR Credential/Secret 边界；
- 不进入 Prompt、浏览器明文、Tool 展示或普通日志；
- 本阶段不写入/删除知识库、不修改共享权限、不自动上传附件。

开发时使用真实测试凭据完成一次端到端验证，并记录当时可用 API、权限、额度和计费观察；不得把“永久免费”作为产品合同。

## 10. Chat UX 与 i18n

M5.7 Chat UI 至少提供：

```text
Thread
Message
Composer
Markdown / GFM
Code Block
Copy
Auto Scroll
Scroll-to-bottom
Running / Loading
Regenerate
Attachments
Tool Call 展示
```

输入行为：

```text
Enter：发送
Shift + Enter：换行
Ctrl/Cmd + Enter：兼容发送
```

用户手工上滚查看历史时不得被新内容强制拉回底部；回到底部后恢复跟随。

M5.6 已建立的 `zh-CN / en` 国际化合同继续适用于全部新增 UI、错误、附件状态和 Tool 状态；不能新增用户可见硬编码单语文案。

## 11. 推荐实施顺序

```text
Wave 1
assistant-ui + External Store + 基础 Thread/Message/Composer

Wave 2
Regenerate + Attachments + Provider 能力判断 + 大文档 fallback

Wave 3
只读 Tool Call + Prompt/Provider 最小扩展

Wave 4
MCP/Knowledge 边界 + 腾讯 ima POC

Wave 5
全量回归、i18n、视觉、浏览器与安全收尾
```

每个 Wave 应独立 PR、独立 Review、CI 全绿后再进入下一批；不得混入 M6 或 Agent Runtime 工作。

## 12. 最小验收矩阵

至少验证：

1. 普通问答与 `candidate=null`；
2. Candidate → Diff → Apply；
3. Apply 仍只修改 Working Copy；
4. stale Candidate；
5. Secret 真值不进入 Prompt/UI/Tool/日志；
6. Context Snippets 顺序和快照语义；
7. Adapter switch / late response 隔离；
8. recent_messages 上限不回退；
9. Regenerate 使用原轮冻结上下文；
10. 图片原生能力与不支持 Vision 的明确失败；
11. PDF / DOCX 原生文件能力与 DLR fallback；
12. 大文档不全文无界注入；
13. Tool Call 只读 allowlist、次数/超时/结果大小限制；
14. Tool 最终回到合法 AiModelOutput；
15. ima 知识库 list/search/read POC；
16. zh-CN / en；
17. Enter / Shift+Enter / Ctrl/Cmd+Enter；
18. 手工上滚与 Scroll-to-bottom；
19. 1280 / 1440 / 1680 / 1920 核心布局；
20. Backend / Web / Compose / GitHub Actions 全绿；
21. 独立 Review 无 Critical / Important。

M5.7 自动开发完成后不得自动关闭 #80，也不得开始 M6；等待用户最终人工验收。
