# Issue #213：B0 首批兼容实验与决策

> 2026-10-21 续作：维护者已授权在 PR #217 依序完成 B0–B5，全部完成后集中 review。以下首批结论保留为历史记录；后续修复须以新增兼容断言证明，不将已知缺口观察用例通过改写为门槛通过。

## CI 首次失败与修复

- 首次 HEAD：`cf773b0cf604f012b22cd284e188f70273b50814`；[CI run 39530948715](https://github.com/john-ops-lab/DataLinkRuntime/actions/runs/39530948715) 后端结果为 1 failed、2867 passed、17 skipped。
- 唯一失败：`test_cancel_reaps_entire_group_after_launcher_exit[False]` 在子进程创建 PID 标记文件后、写入完成前读取，`int('')` 抛 ValueError。生产运行路径未参与本次首批修改。
- 修复：测试子进程先写临时文件并关闭，再通过 `os.replace` 原子发布完整 PID；原有进程组与后代回收断言保持。
- 本地相称验证：`test_issue202_document_worker.py` 36 passed；Ruff 检查及格式通过。新 HEAD 的完整 CI 结果另行记录，不能用本地通过替代。

## 首批历史结论

**结论：B0 尚未通过，停止扩围至 B1；保留可复现实验供 PR review。** 已证明三协议 SDK 的代表路径、严格解析扩展点、媒体续轮、部分工具控制和正常容量共享可用；未知工具记账和取消后的真实资源生命周期仍有已复现差异。它们说明当前薄桥接不足，尚不能据此断言框架本身无法适配。

本批没有接入生产 AI 路径、修改后端依赖锁、切换默认实现、调用真实付费模型或部署应用。完整迁移的 46 项任务继续按各自门槛推进。

## Baseline

- 初始设计：`7fb4c8fc00a0a167a65a95f18e310b9208f1e3ed`；审查和本批源码基线：`5fc95945b894fbc07aae7fc873aac91a2e26fabc`。
- 已将 #213 最新设计、32 项兼容要求、P/G 门槛及任务同步入版本管理；补入 B5 周期完成定义和 G03/G04 同一扩展的新旧对照。
- [baseline.json](../../tools/ai-migration/baseline.json) 冻结 API、Schema、Provider、预算、Prompt、助手页面、#215 记录及合成图片/PDF/输出材料 hash。初始设计至当前基线的文件变化单独列出。
- #215 的 Go 注释语言、Python 说明/实现、TypeScript 空白金额及中文服务端/英文页面附件 Regenerate 的 `ai_tool_duplicate` 首次失败保留。最新顶部帮助入口在原记录中明确未重跑测试；本批不将其写成已验收。
- 验证环境为 Python 3.13 的独立虚拟环境。模型请求通过真实 OpenAI、Anthropic、Google SDK 发往 loopback 模拟器；FunctionModel 仅用于循环与工具控制，未拿它替代协议证据。

## Dependencies

候选固定为 `pydantic-ai-slim[openai,anthropic,google]==2.57.0`，SDK 锁为 OpenAI 3.35.1、Anthropic 1.17.0、Google GenAI 2.32.0；完整传递依赖见独立 [uv.lock](../../tools/ai-migration/uv.lock)。

**首次解析失败：** 原后端锁中的 `websockets==17.0.1` 与候选 Google SDK 的 `websockets<17` 不相容。为继续隔离实验，独立项目只将该传递依赖固定为 16.0，其他已有包受原锁版本约束。该组合完成安装、三 SDK 导入及相关严格类型检查；不能将其写成“现有后端锁原样兼容”。

本机安装新增 25 个发行包，新增包文件逻辑大小约 40.4 MiB；另有一项原包版本变化。此大小来自安装元数据，不是 Docker 镜像实测增量，也不包含包管理缓存。许可证元数据与变化包逐项保存在 baseline.json；tiktoken 的 MIT 许可已核对安装分发中的 LICENSE。正式引入前仍需对这一依赖变动完成项目门禁。

## Public API mapping

以下公开符号均在锁定环境中实际调用；并未修改 SDK/框架私有属性、打补丁或 fork。

| 要求 | 扩展点及实际阶段 | 本批证据与限制 |
|---|---|---|
| P0/P1 | `OpenAIProvider(openai_client=...)`、`AnthropicProvider(anthropic_client=...)`、`GoogleProvider(http_client=..., retry_options=...)` | 显式凭据/client/端点；SDK 重试关闭。原后端锁有 websockets 冲突 |
| P1 | `OpenAIChatModel`、`AnthropicModel`、`GoogleModel`；`NativeOutput` / `PromptedOutput` 与明确 profile | 三协议 prompt/native 代表往返及 OpenAI json_object；实际请求不增加输出工具、不暴露遗留配置字段。完整能力组合留待后批 |
| P2 | `httpx2.AsyncBaseTransport.handle_async_request`、有界 `Response.aiter_raw()` | SDK 解码前复用 DLR strict JSON，拒绝外层重复 key、NaN 和 surrogate。传输守卫仍有下述 DNS 生命周期缺口 |
| P2/P7 | `AbstractCapability.before_output_validate` | 最终文本在框架解析前检查重复键、类型、额外字段、Unicode 和合成 secret reflection；非法结果不触发自动纠正 |
| P4 | `AbstractCapability.after_model_request` | 可在工具执行前整批检查；接收的是 ModelResponse，不能替代 HTTP 原始字节检查 |
| P4 | `Tool.from_schema`、DLR dispatcher、`ToolManager.parallel_execution_mode("sequential")` | 已知工具可沿用严格参数与记账；串行、batch 越限、重复、三次失败及半批停止有实测。未知工具更早被框架拒绝 |
| P5 | 共享 DLR 传输 semaphore + 注入 transport | 37 个合成占位和三协议各一条真实 SDK 传输共同占满 40；第 41 条和既有 Catalog 路径在发 HTTP 前超时。正常结束后 40 个许可全部归还 |
| P5 | `UseThreadExecutor`、`asyncio.Task.cancel()` | 可使用受控 executor，但取消后同步 handler 继续存活；并不自动接管 Reader pin 或底层真实工作 |
| P6 | `RunContext.deps`、显式 client 的 async context | 两个合成用户/Adapter/scope 状态独立，执行时可观察 ACL 拒绝。尚未证明真实 PostgreSQL 短 Session、凭据快照及 prepared Reader 生命周期 |

官方依据：[v2.57.0](https://github.com/pydantic/pydantic-ai/releases/tag/v2.57.0)、[hooks 源码](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/capabilities/abstract.py)、[ToolManager 源码](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/pydantic_ai_slim/pydantic_ai/tool_manager.py)、[工具文档](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/)。滚动文档只作导航，本批以锁版本代码和实际轨迹为准。

## Observed gaps

### B0-U1：未知工具绕过现有桥接的预算与错误语义

复现：`test_unknown_tool_does_not_reach_public_validation_hook_and_is_not_dlr_compatible`。

同一个未知调用到达 DLR 原 dispatcher 时，返回 `ai_tool_unknown`，`record_execution()` 将调度预算和连续失败各加 1。代表性 Agent 桥接下，框架在解析工具名称时先拒绝，`before_tool_validate` 不运行；禁用默认重试后整轮抛 UnexpectedModelBehavior，DLR 调度预算仍为 0。

不能靠提高框架重试、忽略计数或修改内部缓存绕过。下一份最小证据需通过受支持的公开控制方式，保持稳定结果、知识阶段先后顺序、旧计数以及继续/停止路径，并证明没有重写通用循环。本批只证明薄桥接未满足，尚未证明所有公开接入方案不可行。

### B0-R1：取消不代表 DNS 或同步工具真实结束

复现：

- `test_cancelled_dns_outlives_transport_and_thin_wrapper_releases_capacity_early`：真实 SDK 经注入 transport 进入受控慢 DNS；取消后 DNS 仍存活，但薄包装的 finally 已释放许可，后续请求可重新占用该许可。这违反 C26 的真实终态要求。
- `test_cancelled_sync_tool_needs_an_owner_beyond_agent_and_request_finally`：通过公开 UseThreadExecutor 执行同步工具，Agent 任务取消后 handler 和合成 pin 仍存活，直到实际工作结束。测试由 handler 负责保持并释放 pin，未证明生产 Reader 已完成相同接入。

下一份最小证据需建立跨请求 loop 的工作生命周期所有者，证明物理工作结束前不归还许可/pin、响应和清理都有界，并覆盖撤权及短 Session。不得简单在请求 finally 中释放，也不能让 loop 关闭无界等待同步工作。本批 fixture 在结束时释放并等待所有工作，未遗留线程或借用许可。

### 其他已测差异

- 当前 Anthropic 接入使用 SDK beta messages 路径，实际 URL 含 `?beta=true`；不是旧路径完全相同的证据，自定义网关兼容仍待后批验证。
- 未明确 max_tokens 时，候选的 Anthropic 默认行为可能为普通 run 打开内部流。实验显式使用旧路径的 `max_tokens=4096` 后核对 JSON 往返；不能照搬框架默认值。
- Gemini SDK 的部分媒体采用 URL-safe base64。媒体断言解码真实 wire 后核内容 hash、数量和 tool-call 关联，没有仅凭字符串相同认定媒体保真。
- 初版实验夹具的 instrument 参数、工具名/ID 顺序、并发模拟器响应归属及错误字符串预期已修正；它们属于实验实现问题，未登记为 DLR 产品失败，也不改写上述首次依赖失败。

## Gate status

| 门槛 | 本批结果 |
|---|---|
| P0 | 部分：候选隔离安装/导入/类型检查可用；原锁不相容，依赖差异未完成正式接入门禁 |
| P1 | 代表协议/模式/URL/鉴权往返通过；完整三输出模式组合、14 配置映射与真实网关未验 |
| P2 | 原 envelope 与最终内容代表拒绝路径通过；原始工具参数全路径、一次无工具纠正尚未完整证明 |
| P3 | 三协议输入 PNG/PDF、工具页图续轮的内容 hash/数量/关联通过；不表示真实附件转换或内容阅读验收 |
| P4 | 未通过：B0-U1；知识顺序、补读、纠正/收尾及所有停止路径仍需补齐 |
| P5 | 未通过：B0-R1；正常容量共享通过不能替代取消后的真实终态证明 |
| P6 | 部分：合成请求态/ACL 隔离通过；真实 Session、Reader、凭据及撤权可见性尚未齐备 |
| P7 | 部分：三 SDK 429/503 各一次请求、非法 final 无自动纠正已测；完整纠正与审计 canary 覆盖未齐备 |

本批 53 个观察用例包含 3 个 `gap` 用例。全部观察通过仍不满足 B0 继续条件。复现命令见 [实验 README](../../tools/ai-migration/README.md)；Hosted CI 结果归属于实际 PR HEAD，不以本地记录代替。

## Maintenance value and next decision

| 门槛 | B0 初评 |
|---|---|
| G01 | 多 batch 的通用迭代确实由 Agent 驱动；未知工具与停止收尾的适配尚未证明无需重复循环 |
| G02 | 三 SDK 已承担代表协议编码/解码；生产代码尚未删除，不能按文件行数宣称减负 |
| G03/G04 | 本批规划已要求同一工具/模型配置的新旧对照；实际维护成本演示留在 B3，不将框架注册成功算收益 |
| G05 | 实验运行代码只使用框架公开 API；DNS monkeypatch 仅为故障注入。双 httpx 依赖、SDK beta 路径和默认行为增加接入审查成本 |
| G06 | 净收益未证实；未知工具及资源所有权的额外机制可能抵消循环/协议替代收益，需要进一步证据 |

**决策为暂停扩围，不是终止框架方案。** 后续工作先限定于 B0-U1、B0-R1 及它们直接影响的缺口；通过后再补齐剩余 P 项、重审 G 结论和工期。若只能靠私有补丁、重写通用循环/传输栈或长期双实现才能维持，按设计停止全量迁移并另审较小范围。

原 3–5 / 25–40 人日仍是待校正估计。当前没有实测依据确认完整迁移工期或维护收益；本批未支付模型调用费用，未启动 80 样本对照、全语言 Worker 或 UI 矩阵。后续扩大范围需先固定实验范围与投入上限。
