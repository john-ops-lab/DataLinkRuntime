# Issue #213：B3 Agent、领域策略与资源边界

## 结论与范围

B3 基于 B2 `d3ceaa23f3f8f9de4ebb2a3cd96087fb44bf5713`，实现 `PydanticAiRuntime`，默认仍为 legacy。Agent 负责模型迭代、串行调用和结果配对；DLR 保留严格解析、D6 记账、证据完成判定、总预算、权限和资源所有权。没有新增 API、数据库迁移或 UI 行为。真实服务、Chrome、回退及默认切换留给 B4，不能用本批合成结果替代。

维护者于 2026-10-21 明确取消本轮 Anthropic/Gemini 真实在线验收；三协议功能与合成合同保留，两个协议标为“未做真实服务验证”。当前配置的 MiniMax/Kimi 在线验收及其余门槛不变。

## 验证结果

| 入口 | 结果与边界 |
|---|---|
| 原领域断言经真实 SDK 的 opt-in parity plugin | **658 passed**；只替换 HTTP fixture，保留生产 transport、SDK、Agent、原严格及领域断言 |
| 新运行器/资源/协议/扩展/性能/审计终态/语言扫描 | **104 passed**；另补文字+工具同响应，三协议 **3 passed** |
| 同一个合成只读工具与同协议模型扩展 | **4 passed**，两种实现都无需修改主循环；不发布演示工具 |
| 单进程 40 个实际 SDK 工作跨 loop 占位、第 41 个等待 | **1 passed**，与 Catalog 共用许可，实际结束后归还 |
| 静态检查 | 全后端 Ruff/format、strict Mypy（200 source files）通过；SDK 类型仅在专用接入目录 |
| 精确 HEAD CI | 提交后等待。B1/B2 的后端首败为抽取后中文文案不在原扫描许可内；本批抽成专用 canonical `ai/i18n.py`，policy/runtime/presentation 继续扫描，未整体豁免业务目录 |

复现 parity：在 `backend` 中配置项目规定的 PostgreSQL/RabbitMQ 测试环境，执行 `PYTHONPATH=tests:src uv run pytest -p ai_runtime_parity -q`，文件列表见 CI 的 `AI runtime domain parity through official SDKs` 步骤。该插件只用于领域 fixture；真实网络合同用例不能加载它。

### 本地固定延迟对照

同机、同协议、本地 5ms 固定延迟、每样本一次真实文档工具、每组 16 样本，先暖机；两种实现均为每样本两次 HTTP。门槛沿用旧 p95 的 `1.2 倍 + 100ms`。

| 并发 | legacy p50/p95 ms | 新实现 p50/p95 ms | p95 上限 ms | 资源终态 |
|---|---|---|---|---|
| 1 | 47.883 / 59.817 | 57.176 / 66.572 | 171.780 | network/tool work=0，借出 Session=0 |
| 8 | 102.381 / 147.743 | 116.088 / 195.998 | 277.292 | network/tool work=0，借出 Session=0 |

这是本地开销测量，新实现稍慢，不宣称模型质量或速度提升。部署启动/RSS/转换进程等观察留给 B4。B0 的同源依赖镜像增量约 17.1MiB，正式锁与构建见 B2。

## 实现边界

- `Agent.run`、公开 capability、`WrapperModel`、`ToolManager.parallel_execution_mode('sequential')` 管理通用循环。`DomainControl` 仅在公开生命周期检查 DLR 的整批预算、知识/附件进度和审计。停止后的单次无工具收尾、一次格式纠正是有界领域分支，没有通用递归循环。
- 真实 wire 仍提供原白名单 schema/name/id；内部唯一 dispatch bridge 接收所有原参数，未知工具和坏参数不会被框架吞掉。原始重复键、非对象参数及 Unicode 在 SDK/框架 coercion 前检查；框架内使用唯一 ID，实际 wire 原 ID 保留。
- 旧最终输出中的合法配置回显继续按原字段 presence 校验；新 transport schema 只允许代码候选。原文中的 `<think>` 字面量不会被 SDK 推理提取误删。Anthropic 私有签名不进入用户输出或审计。
- 每轮把公开消息映射到原 context-budget 策略，成组裁剪同步更新当前与后续视图；实际 SDK body 再检查字节/媒体/schema 和 deadline。安全错误、传输错误不触发格式重试。
- 网络工作和工具工作分别有有界所有者。超时后真实工具继续持有自己的短 Session、Reader pin 和文件，完成才释放，迟到结果不能修改计数或成为候选。请求返回前再次验证当前账号 Session、启停、角色及 Adapter 权限；独立 PostgreSQL 用例覆盖停用、登出和降权。
- `DLR_AI_ASSIST_RUNTIME=legacy|pydantic_ai` 为过渡部署开关，非法值启动失败；单请求选择一次，无自动后备/重复调用。非 AI 入口不要求导入新框架。

## 首败与修复

原失败日志保留在私有验收记录，未改成 xfail 或删除原断言。主要问题及修复：

1. 框架要求调用 ID 唯一，而原 fixture 可重复 ID；内部关联 ID 唯一化，原 wire ID/参数保留，重复指纹仍按 DLR 原规则拦截。
2. 原生协议的非对象参数被 SDK 归一化；公开 transport 捕获原参数，在 dispatcher 记真实失败后才构造协议合法回送，签名受影响时拒绝。
3. SDK 识别 `<think>` 会破坏合法代码字符串；关闭该 profile 的文本推理提取，不改变原代码。Gemini 使用 proto alias 与 URL-safe base64，媒体断言改为 MIME 与解码后的独立字节真值，旧实现的原断言保留。
4. 两个审计错误分支漏记 Provider 结果；补齐公开 error hook。候选在最终权限检查期间超过总时限会被拒绝；取消只记一个真实错误终态。
5. 运行中停用/登出/降权首轮仍返回 200；改为用新 Session 重新解析当前身份及权限，分别返回原 401/404 语义。
6. 纯领域 harness 跟随共享 preparation seam，提供无数据库的资源 fixture；实际线程 Session/Reader 所有权由独立 PostgreSQL/真实 store 用例验证，没有生产空 Session 后备。

## G01–G06 实现级证据与继续条件

| 项 | 当前证据与结论 |
|---|---|
| G01 | 原 `_assist_impl` 主循环的最终删除对象已明确。新 Agent 迭代不含通用 `while`/递归；保留分支对应 D6、严格纠正与领域收尾 |
| G02 | 三协议编码、媒体/工具 continuation 由官方 SDK 承担；B5 删除旧 Assist 编解码，保留 Catalog、raw 安全检查、配置映射与 URL 边界 |
| G03 | 相同合成只读工具：旧/新均只需 handler+ToolSpec 注册和两项共同合同断言，runtime/codec 零修改。证明局部化，不能声称比旧实现少一步 |
| G04 | 相同已有协议模型：旧/新均通过同一配置入口，仅 model/base_url 与合同断言变化，runtime/codec 零修改；不等于新协议零成本 |
| G05 | 固定 2.57.0 和三 SDK；公开 capability/model/client/transport 接口，无框架私有补丁或 fork。升级需重验 raw 阶段、message/history 语义、SDK wire、生命周期和 parity |
| G06 | 新增接入约 1,633 行，包含传输/资源/消息桥接等必要成本，共存阶段总代码增多。主要收益是删除手写循环与三协议编解码、由 SDK 跟进协议；性能有小幅开销。不能用 LOC 单独宣称净收益，B4 独立审查与 B5 实际删除清单继续核对 |

允许进入 B4 的本地技术依据已具备；提交 CI、真实服务、产品链路、回退和切换后覆盖仍须逐项完成。#202 的数量/截断质量与 #215 历史首败继续单独记录，不通过放宽保护或改变 Prompt 美化迁移结果。
