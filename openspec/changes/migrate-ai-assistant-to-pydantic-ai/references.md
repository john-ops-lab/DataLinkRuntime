# References

本次更新日期为2026-10-21。DLR固定源码/文档核对与Pydantic官方滚动文档阅读不等于运行验证；滚动页面可能晚于原候选版本。原规划的外部研究结论继续作为线索，B0必须对所锁定版本验证公开接口。

### DLR 固定来源

本次审查锚点：`5fc95945b894fbc07aae7fc873aac91a2e26fabc`；初始设计锚点：`7fb4c8fc00a0a167a65a95f18e310b9208f1e3ed`。

| 来源 | 用途 |
|---|---|
| [services/ai.py](https://github.com/john-ops-lab/DataLinkRuntime/blob/5fc95945b894fbc07aae7fc873aac91a2e26fabc/backend/src/dlr/control/services/ai.py) | batch、dispatch记账、知识状态、收尾、最终任务状态 |
| [ai/tools.py](https://github.com/john-ops-lab/DataLinkRuntime/blob/5fc95945b894fbc07aae7fc873aac91a2e26fabc/backend/src/dlr/control/ai/tools.py) | 白名单、原始参数、未知/坏参数结果、handler、截断 |
| [ai/providers.py](https://github.com/john-ops-lab/DataLinkRuntime/blob/5fc95945b894fbc07aae7fc873aac91a2e26fabc/backend/src/dlr/control/ai/providers.py) | 三协议、strict JSON、continuation、单进程共享容量和传输生命周期 |
| [#215注释语言规格](https://github.com/john-ops-lab/DataLinkRuntime/blob/5fc95945b894fbc07aae7fc873aac91a2e26fabc/openspec/changes/issue215-ai-assistant-ux/specs/ai-code-comment-language/spec.md) | 系统语言、完整候选中复制注释本地化，不授权无关业务变更 |
| [#215验收记录](https://github.com/john-ops-lab/DataLinkRuntime/blob/5fc95945b894fbc07aae7fc873aac91a2e26fabc/docs/testing/issue215-ai-assistant.md) | 只读预览/最新帮助入口、注释/业务失败、附件Regenerate的ai_tool_duplicate及证据限制 |

其他基线文件在仓库中按相同SHA核对：`control/api/ai.py`、`control/schemas/ai.py`、`ai/prompt_builder.py`、`ai/context_budget.py`、附件转换/reading/store与旧attachments、`common/config.py`、`backend/pyproject.toml`、`backend/uv.lock`、AI/模型设置前端。README/产品文档如滞后，以现行代码和已确认规格为准，不顺带改写历史。

历史决策：`issue166-remove-ai-saved-sessions` 不得被旧#151/#161恢复聊天规划覆盖；`issue202-multimodal-attachments`、`issue203-204-ai-attachment-reliability`和#215共同构成附件、证据、意图、恢复与当前产品边界。#202/A16和#215的首次FAIL保留原档；缺原材料/响应时如实标明，另建复现实验，不补造历史证据。

### Pydantic AI 官方参考

| 来源 | 核对目的 |
|---|---|
| [原候选 v2.57.0](https://github.com/pydantic/pydantic-ai/releases/tag/v2.57.0) | 原规划研究版本；不视为DLR已验证，不假设滚动文档所有API存在于该版本 |
| [Agent](https://pydantic.dev/docs/ai/core-concepts/agent/) | run/run_sync/iter与取消入口 |
| [Output](https://pydantic.dev/docs/ai/core-concepts/output/) | NativeOutput/PromptedOutput/ToolOutput实际协议差别 |
| [Hooks](https://pydantic.dev/docs/ai/core-concepts/hooks/) | hook阶段/次序、ModelResponse、消息视图和ModelRetry历史行为 |
| [工具API](https://pydantic.dev/docs/ai/api/pydantic-ai/tools/) | from_schema校验责任、公开validator与串行语义；以锁定版本核实 |
| [Advanced tools](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/) | 工具失败/重试/取消及同步线程限制 |
| [Timeouts](https://pydantic.dev/docs/ai/core-concepts/timeouts/) | 单步与整轮时限、SDK重试及同步工作真实生命周期 |
| [Custom capabilities](https://pydantic.dev/docs/ai/capabilities/custom/) | 领域策略公开接入方式，不能据此直接认定无需额外适配 |
| [Compatible APIs](https://pydantic.dev/docs/ai/models/compatible-apis/) | 自定义URL与profile |
| [MoonshotAI](https://pydantic.dev/docs/ai/models/moonshotai/) | 平台接入线索，Kimi Code仍独立验证 |
| [Version policy](https://pydantic.dev/docs/ai/project/version-policy/) | 版本策略不替代锁版本和协议回归 |
| [Harness](https://pydantic.dev/docs/ai/harness/) / [Durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/) | 可选未来能力，本次不引入或承诺 |

### PowerContext 原研究参考

保留 `powercontext-v1.3.0` 的 [RFC0016](https://github.com/oceanbase/powercontext/blob/powercontext-v1.3.0/docs/zh/rfcs/0016_pydantic_ai_inference_integration.md)、[集成文档](https://github.com/oceanbase/powercontext/blob/powercontext-v1.3.0/docs/zh/development/pydantic-ai-inference.md)、[inference实现](https://github.com/oceanbase/powercontext/blob/powercontext-v1.3.0/src/powercontext/builtin/inference/pydantic_ai.py)、[依赖清单](https://github.com/oceanbase/powercontext/blob/powercontext-v1.3.0/pyproject.toml)。参考其模型/领域隔离、显式客户端、候选校验和错误转换，不照搬重试/Memory/存储，不推导整个OceanBase产品均采用同一引擎。

模块布局、预算、批次、G收益要求、暂停/停止和回退均为DLR设计决定，外部文档不保证它们已经可行。只有锁定版本上的可复现实验和分批验收可以给出实现结论。
