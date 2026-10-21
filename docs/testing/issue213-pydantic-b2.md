# Issue #213：B2 三协议 SDK 与传输合同

## 结论与范围

B2 在 B1 提交 `afba0f4021d3383978b6e4425089dc47e2a01eb8` 上接入固定依赖、官方 SDK factory 和公开传输守卫。默认 Assist 仍为 legacy，本批没有模型在线验收、默认切换或数据迁移。三个协议的本地合同证明真实 SDK 编码/解析与错误边界，不能代替 B4 的实际服务证据。

依赖锁 SHA-256：`156d5194ffe8dc239e192a414e8c0f51b8de4f90d819288f20d7034e41c34a46`。固定 `pydantic-ai-slim[openai,anthropic,google]==2.57.0`，`openai==3.35.1`、`anthropic==1.17.0`、`google-genai==2.32.0`；Google 的 `<17` 约束要求 `websockets==16.0`，其余已有版本保持。依赖许可证与同源镜像增量见 [B0 报告](issue213-pydantic-b0.md)。

## 当前证据

| 检查 | 结果与边界 |
|---|---|
| `test_ai_sdk_contract.py` | **62 passed**，通过真实 SDK 请求本地 HTTP 服务；非 TestModel |
| 设置、Provider、模型、连接、reasoning 现行回归 | **116 passed / 65 deselected**，保留 Catalog 和连接测试旧实现 |
| strict Mypy / Ruff / format | 新接入目录通过；SDK 导入限于该目录，业务 DTO 与资源接口没有框架类型 |
| 干净锁安装 | `uv sync --frozen` 成功，三 SDK 版本与 B0 一致 |
| 正式 `docker/control.Dockerfile` | `uv sync --frozen --no-dev` 构建成功；第一次因 Colima 解析 PyPI 失败，单次构建参数提供域名地址后成功；未修改 Dockerfile 或宿主网络 |
| 非 AI 启动/OpenAPI | 构建镜像在禁网模式创建应用；116 个路径，业务 API 不变；部署和实际数据库启动留给 B4 |

合同覆盖三种输出策略、12 个 OpenAI compatible profile、重复 `/v1` 和网关前缀、三种鉴权与显式无 Key、实际图片 bytes、工具 call/result 续轮和 Anthropic 私有签名。没有框架输出工具，Prompt 不重复注入 schema。模型 transport schema 去掉 legacy 配置字段，本地继续用原 `AiModelOutput` 接受并检查合法回显；严格 JSON、类型和 Secret 检查发生在框架最终解析之前。

故障用例验证 401/404/429/503 每次只有一个 HTTP 请求；307 不跟随；外层重复键/非有限数、内部最终 JSON 重复键、错误类型和 canary 被拒绝。实际请求/响应 body 上限均在 SDK 外层。40 个许可占满时，第 41 个请求耗尽原 deadline 且不启动 HTTP。取消请求级 loop 后，尚未结束的真实 body 工作继续占用许可，待实际结束才关闭 transport 并释放。

传输不使用 asyncio 的默认 executor。独立、数量受限的 daemon 工作线程持有 future，处理实际 DNS/socket/body；请求取消不取消 future，不提前释放许可。body 读取检查同一 monotonic deadline，客户端关闭延后到实际工作结束。Catalog 继续使用同一个进程级 semaphore。

## 首败及修复

1. 首轮新用例因旧版 `instrument=` 构造参数在 2.57.0 不存在而失败，未发出 HTTP；改为公开 `agent.instrument = False`。
2. `WithJsonSchema` 的独立 `$defs` 不能直接注册到 Pydantic schema generator，出现 `KeyError`；将仅用于 transport 的闭合 schema 展开引用，未复制业务 DTO 或修改本地解析。
3. 307 在部分 SDK 中被包装为不带状态的连接错误；在公开 transport 直接拒绝重定向并保留真实 HTTP 状态。最终 62 项通过，原失败没有改成预期成功。

## G02/G05 与后续门槛

SDK 负责三协议消息、工具结果、媒体和 continuation 编解码。新增代码保留 DLR 必需的 URL/profile、严格 raw 检查、容量、deadline 和安全错误映射。共存阶段 raw envelope 仍调用原严格检查，B5 删除清单必须区分安全检查与重复编解码。

B3 接入 Agent 后再验证完整工具预算、知识/附件领域状态、每轮上下文预算与实际 SDK payload、审计及有限收尾。B4 仍须逐配置登记实际在线验证，不能把本表写成全部模型/协议已在线支持。
