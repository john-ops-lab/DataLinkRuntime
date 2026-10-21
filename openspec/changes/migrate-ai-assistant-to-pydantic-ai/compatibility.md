# Compatibility

以开工时冻结的实施 SHA 为最终基线，保留初始设计和本次审查 SHA 的差异记录。以下保持条件已按本轮覆盖与证据复用规则完成分批验收，当前结果见 [B4](../../../docs/testing/issue213-pydantic-b4.md) 与 [B5](../../../docs/testing/issue213-pydantic-b5.md)。原模型首败、事实质量限制及 Anthropic/Gemini 在线未验分别保留，不把兼容验收写成全部模型答案正确。

| ID | 功能/边界 | 必须保持及验收重点 |
|---|---|---|
| C01 | HTTP/DTO | 原 assist、设置、模型列表、测试连接、附件路由/字段/未知字段拒绝；快照 OpenAPI、状态码、错误码和安全参数，不暴露框架对象 |
| C02 | 权限 | adapter edit、设置权限、owner/scope、归档/撤权；未授权零 Provider 请求，读取中和返回前按当前权限复查 |
| C03 | 模型设置 | 14 Provider 标识、准确模型、温度/reasoning/默认值/密钥引用，保存格式不变，不从环境找备用模型 |
| C04 | 自定义服务 | 三协议、base URL、网关前缀、能力声明；不重复拼 v1、不吞路径、不偷换端点 |
| C05 | Catalog/连接测试 | 草稿独立可测、模型列表和错误语义保留；HTTP 200 不等于工具/结构化能力已验证 |
| C06 | 凭据 | Secret Store、引用、遮罩、按实际值脱敏；不重录、不泄漏，单请求配置一致 |
| C07 | 临时聊天 | recent_messages 至多 8；折叠/展开保留，刷新/卸载/账号或适配器切换清理；不增加持久会话/摘要 |
| C08 | 意图/完成 | auto/consultation/code_change 与 answered/candidate_ready/incomplete/unclassified 由 DLR 最终计算，不能由 run.output 或 HTTP 200 推断 |
| C09 | 候选代码 | 非空完整代码、summary、required_secret_keys；requirements/runtime_config 遗留回显须 exact-match，不成为 AI 可写配置 |
| C10 | 用户流程 | 只读预览/Diff 不改 WorkingCopy；显式 Apply 只改 code，Save/Run 独立，迟到结果不覆盖新页面/版本 |
| C11 | 五语言 | Python、JavaScript、Java、TypeScript、Go 当前上下文/语法/运行契约保持 |
| C12 | 请求上下文 | WorkingCopy、base_version_id、至多 20 snippets、授权凭据名称/托管输入元数据；不读托管 Blob，不把旧 Candidate 当当前代码 |
| C13 | 只读工具 | 原白名单/Schema/严格参数/未知工具错误；不增加任意执行、SQL、文件访问或动态注册 |
| C14 | 知识库 | opt-in 和服务端校验、list/search/read/跨库阶段、摘要与全文证据；保留 ima 官方接入/权限，附件不替代检索 |
| C15 | 上传/旧附件 | inline/prepared、数量/尺寸/MIME/Unicode、原回退路径；不新建供应商文件托管或远程 URL 自动抓取 |
| C16 | 附件身份/生命周期 | owner+adapter+scope、冻结 ref/hash/revision、TTL/关闭/过期/pin；不得跨用户读或复用旧内容身份 |
| C17 | 媒体能力 | 按准确模型/端点/配置判断图片/PDF/工具；不按品牌或文件开关升级 native PDF |
| C18 | 媒体与表格 | 原生图片/PDF、页图、文本、Excel 原始值及来源/页号/范围；用真实 SDK body 证明不丢失、不重复 |
| C19 | 阅读门槛 | prepared 需成功 read page/sheet；目录不算阅读；未尝试最多一次预算内补读，尝试失败不获额外机会；附件/知识独立 |
| C20 | Retry/Regenerate | 原请求冻结材料与页面 fencing；换好附件属新请求，不能后台替换原失败材料或污染新请求 |
| C21 | 严格输出 | HTTP envelope、最终 JSON、工具参数的重复键/非有限数/Unicode/类型/额外字段/secret reflection 在正确阶段检查，不靠 coercion 修正 |
| C22 | 格式纠正 | 仅 final_json/output_schema 一次、无工具、不回送非法原文，同预算；安全/配置/传输错误不纠正，框架/SDK 不加额外重试 |
| C23 | 工具记账与边界 | 严格执行 D6 矩阵：未知/坏参数到达 dispatcher 后计预算，知识顺序/重复/batch 拒绝分别计；串行、整批预检查、失败/结果/停止顺序保持 |
| C24 | 上下文预算 | 保留关键 WorkingCopy/问题、原估算和成组裁剪，每次调用复查；包括 schema/媒体/base64 实际载荷；当前请求与后续历史视图一致 |
| C25 | 总时限/收尾 | 一个 hard deadline、30 秒预留含在总量；迟到输出拒绝，只有服务器构造并标记安全的 candidate:null fallback 可按旧规则返回 |
| C26 | 网络/共享容量 | 请求32 MiB/响应4 MiB、禁重定向、错误分类、同进程共享40；跨请求/loop/保留入口不扩容，真实工作结束才释放 |
| C27 | 审计/隐私 | 真实尝试和停止原因、工具摘要/终态，不存原始内容/推理；区分调度预算与 handler，隐私 canary 测试通过 |
| C28 | 前端错误 | 原错误码/安全参数、中英文及摘要；失败/no-candidate 不伪装完成，不直接显示 SDK 异常 |
| C29 | 数据/非 AI | 适配器、凭据、模型、模板、执行历史、Worker 协议不迁移；AI 故障不阻断登录、编辑、依赖和执行 |
| C30 | 并发隔离 | 请求状态/凭据/Reader/计数不串；Session 不跨线程、client 不跨 loop；共享 limiter 和独立请求态各在正确作用域 |
| C31 | 迁移切换 | 请求固定实现，失败不自动重跑；部署开关无数据库含义，回退不重放中间状态，最终删除过渡路径 |
| C32 | #215 产品契约 | 服务端注释语言权威、完整代码中复制注释的本地化、五语言/生成修复/附件/重生成覆盖；只读预览、顶部帮助和最新布局不回退；纳入历史失败基线 |

### 默认值与单位

数值来自原规划及已核对源码；开工时以冻结 SHA 再确认，原允许的部署配置覆盖仍保留，不悄然使用框架默认值。

| 项目 | 保留值/语义 |
|---|---|
| Assist 总时限 | 默认150秒，配置120–180秒 |
| Provider 单请求设置 | 默认180秒，配置10–600秒，实际仍受阶段剩余时间约束 |
| 收尾预留 | 30秒，包含在总时限中 |
| 工具回合/预算 | 最多8个接纳 batch、16次 D6 调度预算；与 handler 启动数分开 |
| 工具并发/失败 | 单请求并发1；连续失败阈值3，知识协议失败/dispatcher 失败按旧分支记账 |
| 工具超时 | 默认每次10秒，具体工具/阶段剩余时限可更严格 |
| 参数/摘要 | 参数字符串通常500；生成摘要400，响应 Schema 可接收600 |
| 单次工具结果 | 裁剪上限4,000字符 |
| 累计结果 | 阈值16,000；实际累加 UTF-8 result_size 字节，旧 chars 命名不改变单位；超限检查顺序保持 |
| Provider body | 请求32 MiB、响应4 MiB |
| Provider 容量 | 单进程共享40个传输许可，实际结束后释放；非每请求/每 loop/跨部署限额 |
| 未知上下文窗口 | 默认32,768，可配置8,192–1,048,576 |
| 上下文预留 | 输出8,192、安全2,048、图片基本预留8,192；PDF 沿用原估算 |
| 估算方法 | UTF-8 JSON字节数/2加固定预留，非 tokenizer 精确计数 |
| 历史/片段 | recent_messages≤8，context_snippets≤20 |
| 上传 | inline+prepared总数≤8，单原文件≤6 MiB，总量≤48 MiB |
| 旧文本提取 | 单文件64 Ki字符、合计256 Ki字符，保留DOCX解压防护 |
| 表格读范围 | 每次矩形范围≤200格，原页号/坐标不变 |
| 派生媒体 | 单个≤10 MiB，图片≤16,000,000像素；转换合计64 MiB硬边界 |
| 临时附件 | TTL 1,800秒，store 512 MiB，derived 64 MiB，prepare timeout 60秒 |
| 转换调度 | 原最多2个活跃转换及有界等待/清理 |

### 完成状态优先级

1. prepared 附件未成功内容阅读，或其知识任务仍未完成：`incomplete`。
2. 否则存在通过领域校验的 Candidate：`candidate_ready`。
3. 否则有效意图为 code_change：`incomplete`。
4. 否则有效意图为 consultation：`answered`。
5. 其余：`unclassified`。

继续执行原候选抑制/证据降级规则，以上顺序不授权返回无证据候选。

### Provider 覆盖

保留 `openai`、`anthropic`、`gemini`、`deepseek`、`qwen`、`kimi`、`minimax`、`glm`、`doubao`、`hunyuan`、`openrouter`、`siliconflow`、`ollama`、`custom_openai_compatible`。逐项记录配置映射/合成协议/真实服务状态；自定义 URL、Ollama 无 Key、需 Key 服务、Kimi 平台和 Code 分别核对。未测不写 PASS，原不支持能力不因框架而宣称支持。

允许内部类/路径、无业务含义请求 ID 和经验证的等价表示变化；自然语言可不同。字段默认值、Prompt 顺序、token/网络次数、错误优先级、媒体位置、输出模式差异必须解释验证。API/数据/授权/预算/证据/临时聊天/资源边界不能静默变化。发现旧缺陷独立登记，不复制安全缺陷，也不混入修复后把效果全部归功框架。
