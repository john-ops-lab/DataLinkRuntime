# Proposal

跟踪 Issue：[\#213](https://github.com/john-ops-lab/DataLinkRuntime/issues/213)。维护者于 2026-10-21 授权在 [PR #217](https://github.com/john-ops-lab/DataLinkRuntime/pull/217) 连续完成 B0–B5，全部完成后集中 review。当前先修复 B0 兼容缺口和 CI 失败；每批保留独立提交和验证证据，依序通过技术、兼容与维护收益门槛。首批隔离实验及暂停结论保留为历史证据，不视为后续门槛已通过。

## Why

DLR 现有 AI 助手自行维护模型协议、工具循环、输出校验、预算与附件证据规则，通用框架代码和产品规则交织，增加了后续接入模型与扩展工具的成本。采用 Pydantic AI，并借鉴 PowerContext 的“框架接入与领域规则分离、模型输出仅为候选”做法，可以减少通用机制的自维护，同时保留 DLR 已验证的功能与约束。

## What Changes

- 初始设计基线为 `7fb4c8fc00a0a167a65a95f18e310b9208f1e3ed`（2026-10-18）；2026-10-21 审查与 B0 实施基线固定为 `5fc95945b894fbc07aae7fc873aac91a2e26fabc`，纳入 #215 最新行为和历史失败。逐步用 Pydantic AI Core 替换 AI 助手内部的模型调用和工具循环。
- 保留现有 HTTP API、配置数据、模型选择、临时聊天、附件范围、知识库行为、代码候选、Diff → Apply → Save → Run 流程。支持范围仍是当前代码中的 Python、JavaScript、Java、TypeScript、Go。
- 将权限、严格 JSON 与候选校验、上下文和时限预算、附件与知识库完成判定、脱敏和错误映射保留为 DLR 领域策略；框架类型限制在内部接入目录。
- 建立一个内部运行实现选择点，在迁移期按部署选择 `legacy` 或 `pydantic_ai`，请求开始后固定选择，不在失败后换引擎重跑。通过逐批验收再切换默认值，最后删除旧循环。
- 复用现有工具和 Docling / Gotenberg / LibreOffice 等附件处理组件，不重做前端、Worker、知识库或文档转换系统。
- 同步详细设计、32 项兼容要求、P0–P7 可行性与 G01–G06 维护收益门槛、验收计划和分批任务。B0 使用隔离依赖、本地协议模拟器及合成素材，交付可运行证据；生产运行路径及后端锁保持原状。
- G03/G04 对同一合成工具、同协议模型配置分别演示新旧接入，比较修改文件、职责、特殊分支与测试投入。B5 完整验收周期以默认切换的精确 SHA、覆盖清单、切换后证据及回归处置定义。

**目标**：减少通用协议与 Agent 循环的维护负担；现有用户配置无需重录；替换过程可分批验收和回退；为后续新模型、受控工具和观测留下清晰入口。

**非目标**：新增聊天记忆、自动保存或运行、多 Agent、任意 Shell、MCP 自动发现、工作流平台、外部模型网关、持久化执行引擎；不借迁移调整已有产品限制；不把 #202 的计数准确性问题宣称为框架替换自动解决。

## Capabilities

### New Capabilities

无。本提案是内部实现替换，运行实现选择仅用于部署与回退，不新增用户能力。

### Modified Capabilities

无。已核对 `openspec list --specs`：当前主规格集中在托管输入和 Worker 输入协议，本轮不改变这些要求，也不将历史 AI 设计复制成新的主规格。`.openspec.yaml` 设置 `skip_specs: true`；现有行为的保持条件集中列在 [compatibility.md](compatibility.md)，作为实施与验收约束。若实施中需要改变外部行为，必须另提有规格差异的变更，不能以本次迁移名义混入。

## Impact

- **后端**：主要涉及 `backend/src/dlr/control/services/ai.py`、`control/ai/providers.py`、`control/ai/tools.py` 和相邻 AI 策略代码；新增内部运行接口及 Pydantic AI 接入目录。API、DTO 和应用服务继续由 DLR 拥有。
- **依赖**：拟引入精确锁定且经过验证的 `pydantic-ai-slim[openai,anthropic,google]` 及必要 SDK；不直接引入整个 PowerContext，不默认引入 Pydantic AI Harness 扩展包或新增运行服务。版本取舍在首批兼容验证后冻结。
- **前端及执行链**：沿用 React 19、Ant Design 5.29.3、ProComponents 2.8.10；Worker、队列、依赖材料、执行历史与适配器配置均不迁移。
- **数据库与凭据**：目标是零数据库迁移、零凭据搬迁；现有 Secret Store 仍提供请求级凭据，禁止 SDK 从默认环境变量另选身份或上报原始内容。
- **部署与回退**：迁移期保留旧实现，部署配置选择新请求的运行实现；回退不需要恢复数据库。重启引起的临时附件范围失效仍按现有机制处理，不能声称完全无感。
- **成本与证据**：先用 3–5 人日验证最难边界，再修订总估计；历史验收和首次失败记录保留，新运行实现单独形成证据。完整投入、门槛和停止条件见 [design.md](design.md)、[validation.md](validation.md) 与 [tasks.md](tasks.md)。
