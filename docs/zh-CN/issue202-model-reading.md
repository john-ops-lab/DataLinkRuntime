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
| MiniMax OpenAI-compatible，api.minimax.cn / api.minimax.io 根或 /v1 | MiniMax-M3 | 是 | 未确认 |
| Kimi Code OpenAI-compatible，api.kimi.com / api.kimi.ai 的 /coding 或 /coding/v1 | k3、k3-256k | 是 | 未确认 |
| Anthropic Messages，api.anthropic.com 根或 /v1 | claude-fable-5-1、claude-opus-5-5、claude-sonnet-5-5、claude-haiku-5-5 | 是 | 是 |
| Gemini generateContent，generativelanguage.googleapis.com 根或 /v1beta | gemini-2.5-pro | 是 | 是 |
| 相同 Gemini 端点 | gemini-2.5-flash | 是 | 未单独核实 |

仅匹配 HTTPS、默认 TLS 端口与精确 Provider/协议/模型/根路径。未知模型、代理子路径或其他组合不会继承品牌能力；DLR不自动切换模型、端点或协议。支持声明与平台实际账户可用性分开：上游拒绝仍明确失败。

依据：[MiniMax 国内图片协议](https://platform.minimax.cn/docs/api-reference/text-openai-api)、[Kimi Code 原生视觉](https://www.kimi.com/code/docs/en/third-party-tools/hermes.html)、[OpenAI PDF输入](https://developers.openai.com/api/docs/guides/file-inputs)、[Anthropic当前模型](https://platform.claude.com/docs/en/models/overview)和[PDF合同](https://platform.claude.com/docs/en/build-with-claude/pdf-support)、[Gemini 2.5 Pro](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro)与[文档理解](https://ai.google.dev/gemini-api/docs/document-processing)。集合需随明确验证更新，不按型号名称前缀猜测。

## 只读工具

- `list_attachments`：当前冻结请求的目录、页数、sheet和覆盖。目录不是已读取证据。
- `read_attachment_page(attachment_ref,page,view)`：单个原始1-based页，view为text/image/both/pdf。pdf仅在native_pdf已确认时调用；both提供结构与真实视觉。空白页不改变后页编号。
- `read_attachment_sheet(attachment_ref,sheet,range)`：精确sheet与大写A1范围，最多200单元格；保留原值、格式、公式与缓存来源，不执行公式或宏。

工具只在本轮有 scoped reader 时提供，禁止路径、URL、Shell、其他会话附件及业务 Managed Input Blob。每次读取由reader复核账号/Adapter/scope、原件hash与表示revision。工具层先检查本轮白名单、参数和期限；媒体来源须匹配请求ref和原页号/range。

普通工具仍以10秒为上限，仅read_attachment_page为OCR等重解析单独允许最多30秒；实际期限取工具上限、Assist剩余总时限、工具阶段剩余时限的最小值。超时期限向转换进程传播，由隔离转换层执行实际取消/kill与回收，不通过续期绕过总时限。

## 真实内容传输与预算

媒体字节放在内部 AttachmentMedia 中，验证MIME魔数、大小、SHA-256、图像尺寸/像素或可信PDF页数；不进入普通工具JSON、浏览器摘要或审计。单页PDF仍关联原件attachment_ref与原页号，媒体hash是派生页文件hash。

OpenAI Chat Completions 的tool只接收文本，先完成全部tool_call/result关系，再附加服务端来源封装与真实user image/file parts；Anthropic用tool_result里的image/document blocks；当前Gemini已核对组合先functionResponse，再追加真实user inlineData。补充user媒体仍是只读工具资料，不取得用户指令权限。[OpenAI工具合同](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Anthropic工具内容块](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。

首次、后续、最终化和一次格式纠正都重新预算。图片用保守视觉保留，PDF按可信页数与文件字节估算；原生输出Schema额外计入schema_tokens。审计只记录 image_count、pdf_page_count、media_bytes 等安全数字计数、阶段和策略，实际payload_bytes来自最终协议JSON的UTF-8编码，包含base64、Schema和工具定义。DLR当前请求体上限32MiB，比部分供应商上限更保守。

可选历史、inline参考或首轮媒体可按完整项裁剪，目录引用保留；后续必要tool-call/result及其视觉材料不拆散，超限给稳定预算失败，不把删尽必要图像后的文本答复当作已完成视觉读取。结构或单元格超限明确partial/truncated并保留来源；原件与未选页/range仍可再次有界读取。

Candidate/Diff/显式Apply/Save/Run、Strict JSON、Secret reflection、Unicode与配置边界保持。真实图片、数字/扫描PDF、XLSX与真正XLS的首轮视觉及业务验收仍须独立记录。
