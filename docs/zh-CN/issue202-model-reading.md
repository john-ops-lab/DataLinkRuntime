# AI 附件：模型能力与有界读取

当前页面临时附件的身份与生命周期以 `issue202-multimodal-attachments` OpenSpec 和附件 API 合同为准。本合同解释模型读取层，不把协议/CI通过当作真实视觉业务验收。

## 实际能力

`selected` 返回当前设置的 Provider、model、protocol、images_native、native_pdf、capability_source 和 DLR 的 max_request_bytes。旧 providers 列表是兼容声明，不能代替当前模型/端点验证。修改模型或端点后必须重新读取 selected。

- `verified_model_endpoint`：精确官方组合的文档能力，不代表本机真实业务验收已经完成。
- `custom_declared`：管理员自定义 profile 的图片声明；files_native 勾选不启用未核实的原生 PDF。
- `unknown`：当前组合没有已核实的视觉输入合同，不能宣称文字降级看到了图表。

已核对的窄集合：

| 协议与官方根路径 | 模型 | 图片 | 原生 PDF |
|---|---|---|---|
| OpenAI，api.openai.com 的根或 /v1 | gpt-4o、2024-08-06/2024-11-20 快照，gpt-4o-mini及2024-07-18快照，gpt-4.1、gpt-4.1-mini | 是 | 是 |
| 相同 OpenAI 端点 | gpt-4o-2024-05-13、gpt-4.1-2025-04-14、gpt-4.1-mini-2025-04-14、gpt-4.1-nano及2025-04-14快照、gpt-4-turbo及2024-04-09快照 | 是 | 未单独核实 |
| MiniMax OpenAI-compatible，api.minimax.cn / api.minimax.io 根或 /v1 | MiniMax-M3 | 是 | 未确认 |
| Kimi Code OpenAI-compatible，api.kimi.com / api.kimi.ai 的 /coding 或 /coding/v1 | k3、k3-256k | 是 | 未确认 |
| Anthropic Messages，api.anthropic.com 根或 /v1 | claude-fable-5-1、claude-opus-5-5、claude-sonnet-5-5、claude-haiku-5-5 | 是 | 是 |
| 相同 Anthropic 端点 | claude-opus-4-5、claude-sonnet-4-5、claude-haiku-4-5及各自20251101/20250929/20251001快照；claude-opus-4-6、claude-sonnet-4-6、claude-opus-4-7、claude-opus-4-8、claude-opus-5、claude-sonnet-5、claude-fable-5 | 是 | 未单独核实 |
| Gemini generateContent，generativelanguage.googleapis.com 根或 /v1beta | gemini-2.5-pro | 是 | 是 |
| 相同 Gemini 端点 | gemini-2.5-flash | 是 | 未单独核实 |
| 相同 Gemini 端点 | gemini-2.5-flash-lite | 是 | 是 |

仅匹配 HTTPS、默认 TLS 端口与精确 Provider/协议/模型/根路径。未知模型、代理子路径或其他组合不会继承品牌能力；DLR不自动切换模型、端点或协议。支持声明与平台实际账户可用性分开：上游拒绝仍明确失败。

图片输入、原生 PDF 与严格结构化输出分别核实。补充精确型号与快照用于保持已有 inline 图片入口，不提升输出策略或推断 PDF 能力；官方 GPT-4.1/mini/nano 别名及2025-04-14快照共六个精确型号采用保守的1M门禁，低于文档中的1,047,576 token窗口，GPT-4o/Turbo仍为128K。这也适用于没有附件的 Working Copy，不能用无关型号的128K上限拒绝合法大代码快照；未知组合继续使用配置的默认预算。已退役或受账户限制的上游模型不会因此恢复可用。

依据：[MiniMax 国内图片协议](https://platform.minimax.cn/docs/api-reference/text-openai-api)、[Kimi Code 原生视觉](https://www.kimi.com/code/docs/en/third-party-tools/hermes.html)、[OpenAI PDF输入](https://developers.openai.com/api/docs/guides/file-inputs)、[Anthropic当前模型](https://platform.claude.com/docs/en/models/overview)和[PDF合同](https://platform.claude.com/docs/en/build-with-claude/pdf-support)、[Gemini 2.5 Pro](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro)与[文档理解](https://ai.google.dev/gemini-api/docs/document-processing)。集合需随明确验证更新，不按型号名称前缀猜测。

精确图片型号与快照依据官方 [GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o)、[GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1)、[GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)、[GPT-4.1 nano](https://developers.openai.com/api/docs/models/gpt-4.1-nano)、[GPT-4 Turbo](https://developers.openai.com/api/docs/models/gpt-4-turbo) 和 [Gemini 2.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite) 页面；Flash-Lite 还明确列出 PDF 输入。

仍提供服务的 Claude 图片型号与4.5别名依据官方[生命周期表](https://platform.claude.com/docs/en/about-claude/model-deprecations)、[型号规则](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions)及其链接的 legacy 型号页面；4.5使用200K上下文门禁，所列4.6及以后legacy型号使用1M。Sonnet 4.5在公告的2026-11-30退役前仍提供服务；不补入已退役4/4.1/3.x或需专门访问资格的Mythos型号，新增legacy组合的原生PDF能力仍单独保守处理。

## 只读工具

- `list_attachments`：当前冻结请求的目录、页数、sheet和覆盖。目录不是已读取证据。
- `read_attachment_page(attachment_ref,page,view)`：单个原始1-based页，view为text/image/both/pdf。pdf仅在native_pdf已确认时调用；both提供结构与真实视觉。空白页不改变后页编号。
- `read_attachment_sheet(attachment_ref,sheet,range)`：精确sheet与大写A1范围，最多200单元格；保留原值、格式、公式与缓存来源，不执行公式或宏。

工具只在本轮有 scoped reader 时提供，禁止路径、URL、Shell、其他会话附件及业务 Managed Input Blob。每次读取由reader复核账号/Adapter/scope、原件hash与表示revision。工具层先检查本轮白名单、参数和期限；媒体来源须匹配请求ref和原页号/range。

表格读取额外提供 `cell_summary`：统计仅覆盖请求的精确 sheet/range，`raw_value` 非 null 且非空字符串才计非空，0、false、空格和公式均计入；合并占位不计入。`formula_cells` / `missing_formula_caches` 基于原件，XLS 无法读取公式文本时为 null。`merged_ranges_scope=requested_range_intersections` 的空列表仅说明该范围未交叉合并区域，不代表整张表没有合并。

单工具 4,000 字符、累计 16,000 字符预算保持。超限按完整 cell 前缀裁剪，保留原件 hash/revision、范围统计与合并作用域，并提供 `returned_cells`、`omitted_cells`、最多两个 `unreturned_ranges` 供补读。范围统计完整不代表逐格明细或全文完整。原件缺失缓存仍为 null，派生页的重算显示不得替代。prepared 正文答复需标注实际读取的来源和页号／范围；完整 inline 的精确提取按用户格式回答。

旧 inline 解析使用每个 Control 进程最多两个短生命周期子进程，等待和读取共用原 30 秒期限；超时后实际 kill/reap 再返回稳定错误并释放许可。Linux 子进程另有 512 MiB 地址空间和 CPU 时间限制，源文件不落临时文件，解析器不继承 Control 凭据环境。[Python 子进程超时合同](https://docs.python.org/3.13/library/subprocess.html)说明超时异常本身不会终止进程；本路径显式终止并回收。

普通工具仍以10秒为上限，仅read_attachment_page为OCR等重解析单独允许最多30秒；实际期限取工具上限、Assist剩余总时限、工具阶段剩余时限的最小值。超时期限向转换进程传播，由隔离转换层执行实际取消/kill与回收，不通过续期绕过总时限。

## 真实内容传输与预算

完整普通 inline 文本可附加最多 8 个 `text_fields` 字面提示（标签 ≤80 字符，值 ≤256 字符，保留原行号和值跨度）。只按首个中英文冒号拆分，不执行、不解码、不推断业务含义；提示不保证穷尽全部字段，截断文本不生成提示。提示和原文均属于不可信参考资料，仍受原有 D6 预算约束。仅提取字段值与复述完整行采用不同输出格式；字段标签不自动属于字段值。

媒体字节放在内部 AttachmentMedia 中，验证MIME魔数、大小、SHA-256、图像尺寸/像素或可信PDF页数；不进入普通工具JSON、浏览器摘要或审计。单页PDF仍关联原件attachment_ref与原页号，媒体hash是派生页文件hash。

OpenAI Chat Completions 的tool只接收文本，先完成全部tool_call/result关系，再附加服务端来源封装与真实user image/file parts；Anthropic用tool_result里的image/document blocks；当前Gemini已核对组合先functionResponse，再追加真实user inlineData。补充user媒体仍是只读工具资料，不取得用户指令权限。[OpenAI工具合同](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Anthropic工具内容块](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。

首次、后续、最终化和一次格式纠正都重新预算。图片用保守视觉保留，PDF按可信页数与文件字节估算；原生输出Schema额外计入schema_tokens。审计只记录 image_count、pdf_page_count、media_bytes 等安全数字计数、阶段和策略，实际payload_bytes来自最终协议JSON的UTF-8编码，包含base64、Schema和工具定义。DLR当前请求体上限32MiB，比部分供应商上限更保守。

可选历史、inline参考或首轮媒体可按完整项裁剪，目录引用保留；后续必要tool-call/result及其视觉材料不拆散，超限给稳定预算失败，不把删尽必要图像后的文本答复当作已完成视觉读取。结构或单元格超限明确partial/truncated并保留来源；原件与未选页/range仍可再次有界读取。

Candidate/Diff/显式Apply/Save/Run、Strict JSON、Secret reflection、Unicode与配置边界保持。真实图片、数字/扫描PDF、XLSX与真正XLS的首轮视觉及业务验收仍须独立记录。

Anthropic thinking/redacted_thinking 与签名仅保留在请求内私有续轮载体，按原始顺序完整回传，不进入 UI、审计或重试 DTO；需安全改写的工具参数不能破坏签名内容，直接拒绝。依据[官方 thinking 工具续轮合同](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows)。

Provider HTTP 使用绝对 monotonic 期限，同时主动 shutdown/close 已建立的 TCP/TLS socket，阻止逐字滴流延长读取；每次调用后和 Assist 返回前再次检查原始总期限。迟到的模型输出不能成为 Candidate 或成功答复。

工具执行后，若最终模型等待耗尽总期限，Assist 仍保留服务器生成的安全降级说明与脱敏工具摘要，candidate=null。仅服务器降级路径设置请求内私有标记，允许这一降级响应通过最终期限门禁；工具停止原因或模型返回 candidate=null 都不能放行迟到的模型正文或代码。

Provider HTTP 每进程固定最多40个传输worker，与此前同步调用入口的默认并发容量相同；等待容量计入原始期限，耗尽仍返回既有ai_timeout。调用方超时会关闭已登记socket，但不提前释放worker名额，只有worker实际退出才释放，避免系统DNS阻塞时重试产生无界线程。Python无法强制终止getaddrinfo，DNS名额可能保留到系统解析返回；不引入新DNS进程或依赖。

Provider 失败保留既有鉴权、模型/路径、超时、图片、推理和模型发现专门错误码；其他上游 HTTP 错误使用 `ai_provider_http_error`（Control HTTP502），公开参数仅可带安全整数 `params.http_status`。收到上游 HTTP 响应不能判为 DNS/TCP/TLS 失败。失败的 `provider_result` 审计事件新增固定 `transport_category`（`http`、`dns`、`tls`、`connection`、`timeout`、`protocol`、`unknown`），仅 HTTP 类别附带有界整数 `http_status`，专门错误码也保留实际 HTTP 状态。原有 `stage`、`reason` 和 schema version 保持兼容，成功事件形状不变；分类只依据异常类型/errno，不解析上游文字。禁止输出 URL、Key、headers、body 或原始异常，诊断不会重试或切换 Provider/模型。旧记录缺少这些字段时，不能只凭 `ai_provider_unreachable` 反推历史根因。依据 [Python HTTPError/URLError](https://docs.python.org/3.13/library/urllib.error.html) 和 [SSL 异常](https://docs.python.org/3.13/library/ssl.html#exceptions)。
