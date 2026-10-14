# AI attachments: model capabilities and bounded reading

The `issue202-multimodal-attachments` OpenSpec and attachment API define temporary identities and lifecycle. This document covers model reading; protocol and CI success do not establish real visual business acceptance.

## Actual capability

`selected` describes the current provider/model/protocol, images_native, native_pdf, capability_source and DLR max_request_bytes. The legacy providers catalog is a compatibility declaration, not verification of the selected combination. Refresh selected after changing settings.

- `verified_model_endpoint`: documentation for an exact official combination; real local business acceptance is separate.
- `custom_declared`: the administrator's custom image declaration. A files_native checkbox alone never enables native PDF.
- `unknown`: no verified visual input contract for this combination. Text fallback cannot claim charts were seen.

The narrow documented set is:

| Official protocol/host/root | Models | Image | Native PDF |
|---|---|---|---|
| OpenAI Chat, api.openai.com root or /v1 | gpt-4o and 2024-08-06/2024-11-20 snapshots; gpt-4o-mini and 2024-07-18 snapshot; gpt-4.1; gpt-4.1-mini | Yes | Yes |
| Same OpenAI endpoint | gpt-4o-2024-05-13; gpt-4.1-2025-04-14; gpt-4.1-mini-2025-04-14; gpt-4.1-nano and 2025-04-14 snapshot; gpt-4-turbo and 2024-04-09 snapshot | Yes | Not independently established |
| MiniMax Chat, api.minimax.cn / api.minimax.io root or /v1 | MiniMax-M3 | Yes | Unconfirmed |
| Kimi Code Chat, api.kimi.com / api.kimi.ai /coding or /coding/v1 | k3; k3-256k | Yes | Unconfirmed |
| Anthropic Messages, api.anthropic.com root or /v1 | claude-fable-5-1; claude-opus-5-5; claude-sonnet-5-5; claude-haiku-5-5 | Yes | Yes |
| Same Anthropic endpoint | claude-opus-4-5, claude-sonnet-4-5, claude-haiku-4-5 and their 20251101/20250929/20251001 snapshots; claude-opus-4-6, claude-sonnet-4-6, claude-opus-4-7, claude-opus-4-8, claude-opus-5, claude-sonnet-5, claude-fable-5 | Yes | Not independently established |
| Gemini generateContent, generativelanguage.googleapis.com root or /v1beta | gemini-2.5-pro | Yes | Yes |
| Same Gemini endpoint | gemini-2.5-flash | Yes | Not independently established |
| Same Gemini endpoint | gemini-2.5-flash-lite | Yes | Yes |

Matching requires HTTPS, the default TLS port, and exact provider, protocol, model and root. Unknown models or proxy subpaths do not inherit brand capabilities. DLR never changes the selected model, endpoint or protocol. Documented support does not guarantee account availability; upstream rejection remains an explicit failure.

Image input, native PDF and strict structured output are separate capabilities. The added fixed IDs keep the existing inline-image path usable; they do not promote the output strategy or infer PDF support. The conservative OpenAI context guard remains 128K for these IDs. Retired or account-restricted upstream models are not made available by this declaration.

Sources: [MiniMax image protocol](https://platform.minimax.cn/docs/api-reference/text-openai-api), [Kimi Code vision](https://www.kimi.com/code/docs/en/third-party-tools/hermes.html), [OpenAI file inputs](https://developers.openai.com/api/docs/guides/file-inputs), [current Claude models](https://platform.claude.com/docs/en/models/overview) and [PDF support](https://platform.claude.com/docs/en/build-with-claude/pdf-support), [Gemini 2.5 Pro](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro) and [document understanding](https://ai.google.dev/gemini-api/docs/document-processing). Update explicit evidence rather than guessing capabilities from model prefixes.

Exact image IDs and snapshots are documented in the official [GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o), [GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1), [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini), [GPT-4.1 nano](https://developers.openai.com/api/docs/models/gpt-4.1-nano), [GPT-4 Turbo](https://developers.openai.com/api/docs/models/gpt-4-turbo) and [Gemini 2.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite) pages; Flash-Lite explicitly lists PDF input as well.

The [Claude lifecycle table](https://platform.claude.com/docs/en/about-claude/model-deprecations), [model IDs](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions) and linked legacy model pages establish served image-capable IDs and 4.5 aliases. Claude 4.5 uses a 200K context guard; the listed 4.6 and later legacy models use 1M. Sonnet 4.5 remains served until its announced November 30, 2026 retirement. Retired 4/4.1/3.x IDs and specialized restricted-access Mythos models are not added. Native PDF remains separately unconfirmed for the added legacy combinations.

## Read-only tools

- `list_attachments`: this frozen request's directory, page/sheet metadata and coverage. A directory is not evidence of reading the content.
- `read_attachment_page(attachment_ref,page,view)`: one original 1-based page; view is text/image/both/pdf. PDF requires verified native_pdf; both combines available structure and real visual material. Blank pages retain their positions.
- `read_attachment_sheet(attachment_ref,sheet,range)`: exact sheet and uppercase A1 range, at most 200 cells. Preserve raw values, formats, formula/cache provenance; never execute formulas or macros.

Only requests with a scoped reader receive these tools. Arbitrary paths, URLs, shell, other scopes and business Managed Input blobs are inaccessible. The reader revalidates principal/Adapter/scope, original hash and representation revision at every read. Tools enforce the frozen whitelist, arguments and deadlines; media provenance must match the requested ref and original page/range.

Existing tools retain a 10-second bound. Only read_attachment_page receives a separate maximum of 30 seconds for OCR/heavy reading. Actual time is the minimum of that bound, the remaining Assist deadline and tool-phase deadline. The converter receives this same limit and owns actual process cancellation/kill and cleanup; no deadline renewal is allowed.

## Real transport and budgets

Binary bodies live in internal AttachmentMedia objects, with MIME magic, size, SHA-256, image dimensions/pixels or trusted PDF page counts checked. They never enter ordinary tool JSON, browser summaries or audit bodies. A single-page derived PDF retains the original attachment_ref/page; its media hash identifies the derived bytes.

OpenAI Chat tool messages accept text only: append all tool-call/results first, followed by server-associated provenance and real user image/file parts. Anthropic embeds image/document blocks inside tool_result. Current verified Gemini combinations use functionResponse followed by real user inlineData. Supplementary user media remains untrusted tool material and gains no user instruction authority. See [OpenAI tool message contract](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) and [Anthropic tool content blocks](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).

Initial, follow-up, finalization and one format-correction calls all recheck budgets. Images receive conservative visual reserves; PDFs use trusted page counts and byte estimates. Native output Schema contributes schema_tokens. Audit records contain safe numerical estimates, stages and strategies only. Actual payload_bytes uses final protocol JSON UTF-8 encoding, including base64, Schema and tool definitions. DLR currently caps request bodies at 32 MiB, below some upstream limits.

Optional history, inline references and first-round media can be omitted as whole items, while directory references remain. Necessary follow-up tool-call/results and visuals remain intact; an oversized group fails with a stable budget error instead of deleting all necessary imagery and claiming visual reading succeeded. Truncated structure/cell data is explicitly partial and retains provenance. Original files and unselected pages/ranges remain available for subsequent bounded reads.

Candidate/Diff/explicit Apply/Save/Run, strict JSON, Secret reflection, Unicode and configuration boundaries remain intact. Real image, digital/scanned PDF, XLSX and genuine XLS first-result visual/business acceptance is still independently required.

Safe audit diagnostics include image_count, pdf_page_count and media_bytes; visual reserves stay in the corresponding message-token category and are not counted twice.

Anthropic thinking/redacted_thinking signatures stay in a private request-local continuation carrier. Echo the complete original content array in order; never expose it to UI, audit, or retry DTOs. If tool arguments require safety rewriting, reject instead of corrupting signed content. See the [official thinking tool-workflow contract](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows).

HTTP transport uses an absolute monotonic deadline and actively shuts down/closes established TCP/TLS sockets, including dripping headers or bodies. Check the original total deadline again after each call and before returning Assist: late output cannot become an accepted Candidate or successful answer.

When the final provider wait exhausts that deadline after tools have run, Assist retains the server-generated safe fallback and its sanitized tool summaries, with candidate=null. Only the server fallback path sets a private request-local marker permitting this degraded response through the final deadline guard. A tool stop reason or a provider's candidate:null never admits late provider prose or code.

Provider HTTP has a fixed per-process limit of 40 transport workers, matching the previous synchronous caller capacity. Waiting for capacity consumes the original deadline and returns the existing ai_timeout when exhausted. A timed-out caller closes registered sockets but does not release its worker's permit; only actual worker termination releases it. This bounds retries when system DNS resolution is stalled. Python cannot force-stop getaddrinfo, so DNS stalls may hold capacity until the resolver returns; no new DNS process or dependency is introduced.
