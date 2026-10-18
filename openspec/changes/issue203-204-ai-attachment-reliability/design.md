# Design

## Context

见 proposal.md。沿用 PromptContext、AiModelOutput、ProviderAdapter、预算与请求局部审计；坏附件目前在调用前拒绝，最新恢复成功，不改附件持久化策略。

## Goals / Non-Goals

Goals：统一业务需求授权与 Working Copy 事实边界；增加可见任务状态；严格协议有限纠正。
Non-Goals：不增加 Agent 框架或依赖，不改变自动化操作权限，不追踪 #129/#202。

## Decisions

- 业务意图由请求的显式 task_intent 元数据承载，默认 auto 兼容旧客户端；模型可选 request_intent 仅依据 USER_REQUEST，无声明且无 Candidate 时返回 unclassified；不通过脆弱关键词推断。服务响应独立 task_status，不向模型扩大输出 Schema。
- Provider 复用薄适配。MiniMax M3 官方开源验证器明确 Chat Completion 不支持 json_object，因此保持 prompt_only；Kimi Code api.kimi.com/coding + k3-256k 的 OpenAI 兼容组合与 Moonshot 平台不同，官方 Code 文档确认端点与模型、未声明该组合 response_format 保证，保守采用 prompt_only，不断言上游拒绝 JSON mode。未知组合保持提示词与本地严格校验，不自动换 Provider。官方 api.openai.com 的 gpt-4o-mini / gpt-4o-mini-2024-07-18 / gpt-4o-2024-08-06 使用原生 json_schema；传输 Schema 从本地模型派生，仅去掉 legacy requirements/runtime_config echo，使对象封闭。本地旧 echo 校验不变，依赖、凭据与配置仍需人工操作。
- 最终 JSON/Schema 失败后加入仅固定阶段/原因的系统纠正提示，不回灌原始坏输出，不执行工具。同请求局部计数最多一次，prepare_call 检查上下文预算并使用硬总时限。Unicode/Secret/配置/传输错误不纠正。
- 保留现有冻结请求与 generation/scope fencing，前端说明 Retry 原请求与新附件新发送差异。

参考：[MiniMax 官方 M3 协议验证器](https://github.com/MiniMax-AI/MiniMax-Provider-Verifier/blob/main/m3_format_check/docs/m3_text_cases.md)、[Kimi 官方长思考协议边界](https://platform.kimi.com/blog/posts/kimi-thinking)。[Kimi Code 官方端点/模型及平台区分](https://www.kimi.com/code/docs/en/)、[OpenAI 官方 Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。不引入 LangChain：仅两个局部薄适配与一次格式纠正，无运行时依赖必要性。

## Risks / Trade-offs

提示词与确定性测试无法证明真实模型首次业务完成 → 主 Agent 固定模型、端点、文件、请求、Working Copy 进行 MiniMax/Kimi 首次对照。
未知组合无法确认原生严格输出 → 保持既有兼容模式与本地强校验，不自动换 Provider。

## Migration Plan

可选 API 元数据，无数据库或部署迁移；代码回滚即恢复原行为。
