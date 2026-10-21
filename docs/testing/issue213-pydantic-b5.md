# Issue #213：B5 删除旧实现与最终确认

B4 的独立退出审查已通过，确认周期覆盖冻结核心对照、当前启用的 MiniMax/Kimi、三协议合成合同、修正版本补验、Chrome 产品链路及新→旧→新回退。范围与首败见 [B4 报告](issue213-pydantic-b4.md)。本批已应用删除补丁并完成本地回归与独立源码审查；精确 HEAD CI、部署及预留 Chrome 烟测完成前，不宣布全部交付通过。

## 实际删除与保留边界

| 对象 | 本批处理 |
|---|---|
| `services/ai.py` 的旧 Assist 实现 | 删除 `_assist_impl`、旧工具调用组装、Provider 审计包装、格式纠正及停止收尾共 6 个专用函数；745 行函数源码 |
| `ai/providers.py` 的重复协议层 | 删除三协议消息/工具/媒体编码、旧 Assist 请求与最终文本提取等 17 个函数；499 行函数源码；另删除无人读取的旧 Anthropic 私有块载体 |
| `runtime/legacy.py` | 删除 `LegacyRuntime`；生产 Assist 仅进入共享准备与 `PydanticAiRuntime` |
| 过渡选择 | 删除 Settings 字段、`DLR_AI_RUNTIME` 的示例/Compose 配置及双实现测试参数；运行时不再选择或静默重跑旧实现 |
| Catalog | 使用公共 SDK `Model.request` 的文本请求，继续沿用原配置/能力/URL、总时限、共享 40 容量及错误边界；不进入 Assist Agent、工具或候选 schema |
| 必要边界 | 保留原始 HTTP/参数/最终文本严格检查、GET 模型发现、领域策略、权限、预算、D6 与真实资源所有权 |
| 迁移工具 | 删除独立 B0 探针包/锁、临时 CI job 及 parity 插件；保留报告、原始失败和必要生产 fixtures |

上述 1,244 行仅为两个文件中被删除函数的源码行数，不包含全部删改，也不作为净收益指标。新 Agent 负责通用迭代，公开 SDK 负责协议编码与 continuation；保留的有界纠正/收尾和 D6 控制有具体领域合同，没有在 hook 中复制旧通用循环。

## 默认回归与历史证据映射

迁移期的领域断言已直接运行生产 SDK 路径。`ai_sdk_fixtures.py` 是单次 HTTP fixture 适配器，不含模型/工具循环；真实网络合同仍使用其独立模拟服务。未以 stub 替代生产资源、权限或计数结论。

| 合同 | 当前回归入口 |
|---|---|
| 三协议 URL/鉴权、原生消息、三输出方式及媒体关联 | `test_ai_sdk_contract.py`、`test_ai_official_input_compat.py`、`test_issue202_model_reading.py`、`test_m5_11_wave_d.py` 与原 API/媒体断言 |
| 一次格式纠正、禁工具收尾、知识纠正 | `test_ai_pydantic_runtime.py` 的三协议代表用例、`test_ai_knowledge.py`；另保留格式/收尾/停止全分支及原失败状态断言 |
| 严格解析/D6/审计/预算 | `test_ai_tools.py`、`test_ai_context_budget.py`、`test_ai_pydantic_finalization_guards.py`、`test_issue204_*` 与实际 wire 预算合同 |
| Catalog 草稿及错误 | `test_ai_catalog_sdk.py` 的三协议真实 SDK 请求、原连接/发现/图像错误用例；草稿不保存设置 |
| 配置/身份/撤权隔离 | `test_ai_runtime_isolation.py` 使用真实 PostgreSQL、生产 ResourceFactory 与 Reader，A/B 独立并发，提交撤权可见，Reader pin 和借出 Session 归零；其它账号 API 合同继续保留 |
| 真实资源及容量 | `test_ai_runtime_resources.py`、`test_ai_sdk_deadline.py`、`test_issue204_provider_transport_diagnostics.py` 和现有真实 DNS/socket/TLS/40–41 请求测试；HTTP 返回与物理工作结束分别断言 |
| 固定延迟开销 | `test_ai_runtime_performance.py` 只测当前 SDK 实现，每档 16 个样本、每样本两次实际 HTTP、网络/工具工作与借出 Session 归零；旧基线固定在 B3 |

B0 的 110 项历史实验、首批 53 项及 3 个 gap 均保留原口径，可经 [B0 报告](issue213-pydantic-b0.md) 的固定提交链接复现。关键协议/纠正/知识/隔离代表合同已经迁入默认回归，不再维持第二套实验实现。Anthropic/Gemini 的合成能力继续保留，本轮未做两者真实服务验证。

SDK 的原生 tool-result 内容容器可能与旧编码不同，例如 Anthropic 文本块列表和 Google `return_value`；当前断言核对完整内容、原调用 ID、媒体 bytes/hash 与关联，不将合法容器差异误判为丢失，也不只检查“请求成功”。

## 验证与收益

本地删除影响首跑 **1109 passed / 1 failed**：唯一失败为原 `test_office_unknown_termination_is_quarantined_and_next_request_recovers[unreachable]` 的进程终止/恢复检查，该文件及转换服务未修改。原失败保留；针对该项的单独复核与最终清理影响的协议/业务检查合计 **73 passed**。现象未在复核中重现，根因未确认，没有改用例、放宽超时或修改转换器。不能把这次首跑写成全绿。

最终树 Ruff 检查/格式（409 个文件）及 strict Mypy（200 source files）通过。独立只读审查确认实际删除、公共 SDK Catalog、共享传输及默认测试迁移无明确阻断，G01/G02/G05/G06 成立；审查未代替运行测试。

| 本地固定延迟 | 当前 p50 / p95 ms | 沿用 B3 的 p95 上限 ms | 真实终态 |
|---|---|---|---|
| 并发 1 / 16 样本 | 57.162 / 68.416 | 171.780 | 32 次 Provider HTTP，network/tool work=0，借出 Session=0 |
| 并发 8 / 16 样本 | 122.659 / 204.046 | 277.292 | 同上 |

两档均未越门槛。此为同机 5ms loopback 场景的本地开销，不是线上模型速度结论。精确提交 CI、部署身份及真实页面结果将在完成后登记；B4 的真实模型首败、额外表格事实问题、资源成本和未做范围保持不变。

G01/G02 的收益是实际移除手写通用循环、重复协议层及长期双实现；G03/G04 原对照两边均无需改 runtime/codec，只能证明扩展局部化。G05 保持公开 API、精确锁和 SDK 类型隔离，升级仍需回归 raw、history/wire 与生命周期。G06 同时承担接入/传输/资源控制、约 432 行测试 HTTP 适配器和 SDK 升级维护；依赖镜像约增加 17.1MiB、真实 Control RSS 约增加 140MiB，不能宣称速度、模型质量或总维护量按行数同比改善。

## 首次完整 CI 揭示的 C24 缺陷

`407ca6ca6293cd02f063e99b7a3fd7231c41b07e` 的首次完整 CI 为后端 **3019 passed / 2 failed / 17 skipped**，其余三项通过；部署未触发。两项失败来自原 Issue #183 预算审计用例，揭示生产裁剪缺陷：SDK 合并相邻 `ModelRequest` 后，系统提示与旧 user 历史共处一条消息。预算删除旧历史 row，但重建仍加入已删除的 part，导致估计 20,368 bytes、实际发送 62,288 bytes。问题在 `70c9651` 新路径也存在，不能归类为只漏迁移测试；此前 C24 完全闭合的判断已收回，修复后再验证。

修正按保留的原消息及 part index 重建已映射片段，保留系统提示、当前 WorkingCopy/问题、工具关系、元数据和原未映射 SDK part 策略。相同夹具首请求降为 15,459 bytes，后续不再重复省略同一历史。没有修改模型、Prompt、锁、窗口或总时限。

强回归 **26 passed**，包含三协议正常续轮/重复停止后收尾的六条真实 loopback SDK 轨迹、合并消息及元数据检查。每轮独立核对原始 SDK body：旧历史/旧回复不存在，完整当前材料保留，审计字节等于实际长度，保守字节估计覆盖 body。用原 `407ca6c` 的生产 bridge 运行九条新增/加强检查，九条全部按预期失败，证明检查能够拦住原缺陷。

原审计 token 精确断言迁到实际保留的公开消息预算视图；SDK 原生 JSON 的 tool-result 字符串容器与 DLR 视图有小量计数差别，不能混作同一口径。deadline 则分别验证：SDK timeout 必须落在 before-hook 调用前后绝对 deadline 的严格时间包络内；实际 OwnedTransport 每轮使用原 tool/hard deadline；审计 elapsed+remaining 符合整数截断关系。没有放宽原 1ms 容忍、篡改 fixture 的 timeout 或增加时限，真实慢 I/O 终止仍由既有物理网络合同证明。

原正式 80 个样本的 **176 次预算检查均无裁剪**，原事实与业务证据可按影响范围保留；不把这些审计冒称在线 wire 抓包。修正后的八个相邻模块合计 **259 passed**，独立只读复核确认 C24 根因闭合、时间断言未放宽。新精确提交的完整 CI 通过后才更新部署。

## 当前运维方式

当前应用只使用新运行器，设置与凭据继续沿用，无数据库迁移。应用版本回退应使用 B4 已验证的 `70c96518ec0f8945436a1a50edeec9a7af3893e9` 配套源码、锁与镜像身份，保留持久卷、Token、Master Key，不恢复数据库。该版本内部的新→旧→新运行器切换已实测；本批没有另做“删除后的新版本→旧应用版本→新版本”演练，不能混写。

若回退至上述迁移期双运行器版本，应使用已验证的 `legacy` 配置：其新路径存在本节记录的裁剪缺陷。当前 B5 版本已没有运行器选择开关；不能将旧版本的新路径称为修正后的运行器。

重启会按原语义使临时附件 scope 失效，需重新上传。使用正常部署入口与其 CI/HEAD 门禁；当前本机控制器只允许最新合格 PR HEAD，本文不授权绕过门禁选择历史 SHA。历史镜像/锁用于受支持的版本回退流程和故障诊断。

最终维护者 review 与自动化技术验收分别记录；本 PR 不自动合并，不删除用户历史验收数据。
