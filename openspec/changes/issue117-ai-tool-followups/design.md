## Context

参见 [proposal.md](proposal.md) 的动机。当前 Control 在 `services/ai.py` 内以一个同步循环调用 Provider；`ai/tools.py` 提供固定白名单、参数校验、顺序执行、结果脱敏和 4 轮 / 8 次预算。预算、累计结果或 Provider 失败目前直接抛出错误，因此 `executed_tools` 尚未进入响应时就会丢失。知识库优先顺序只写在 Prompt 中，Provider 可以不调用工具而直接结束。

现有 `dlr.ai.tools` 以 `INFO` 记录无关联 ID 的单行元数据，但 Control 的有效日志级别不能保证这些记录进入持久文件，也没有单次 Assist 的轮次、调用序号或终止原因。平台已有 bind-mounted `platform-logs/control/`，普通 `*.log` 依赖宿主机 logrotate；本变更需要在应用内保证工具审计本身有界，避免未安装宿主机策略的本地部署无限增长。

Web 的历史和实时日志复用 `OutputView.tsx` 中的 `LogView`。工具栏表面已统一为深色，但按钮前景规则只作用于 `.live-log-workspace` 和最大化状态；执行详情中的非最大化 `.history-log-pane` 因此落回 Ant Design 默认深色图标，形成截图中的低对比度回归。AI 工具卡片还会识别内部截断标记并额外显示双语“已截断”提示。

约束如下：不新增第三方依赖，不引入数据库表或外部日志系统；AI 请求、附件和会话仍只存在浏览器内存 / 当前请求中；Secret、Prompt、reasoning 和原始响应不得落盘；现有 Provider、Candidate、知识源只读白名单和未启用知识库时的请求行为保持兼容。

## Goals / Non-Goals

**Goals:**

- 把 AI 工具循环改成显式、有终态的编排状态，使每个保护边界都能转入一次最终回答，而不是丢弃已有结果。
- 让知识库开关由服务端状态机强制执行证据优先顺序，而不是只依赖 Prompt 自觉。
- 为每次工具尝试提供可按会话 / 请求复盘、立即落盘、默认有界的 JSON Lines 审计事件。
- 以最小 CSS / 组件修改修复历史日志工具栏，并用可计算对比度和真实交互保护实时日志现状。

**Non-Goals:**

- 不把 Assist 改成异步 Job、流式 Agent、并行工具执行或自动重试系统。
- 不保存或恢复 AI 对话，不让 `conversation_id` 成为账号会话、鉴权凭据或数据库对象。
- 不集中采集、搜索或上传审计日志；运维仍通过现有宿主机平台日志边界读取文件。
- 不改变日志正文主题、Execution 日志截断提示、AI Candidate Schema 或 Apply / Save / Run 权限边界。

## Decisions

### 1. 用单次 Assist 状态对象统一预算、截止时间和终止原因

在 `services/ai.py` 的调用边界创建一个请求级状态对象，持有：

- `request_id`、`conversation_id`、`started_at`、工具阶段截止点和硬截止点；
- 已使用工具轮次、已实际调用次数、连续失败次数和累计结果字符数；
- 已完成的 `AiToolCallSummary`、规范化调用指纹集合和当前知识库阶段；
- 单一 `stop_reason`，例如 round / call budget、duplicate、consecutive failures、result size 或 total timeout。

`MAX_TOOL_ROUNDS` 调整为 8，`MAX_TOOL_CALLS_PER_ASSIST` 调整为 16。新增 `DLR_AI_ASSIST_TOTAL_TIMEOUT_SECONDS`，默认 150 秒并通过配置校验限制在 120–180 秒。硬截止前固定保留最后 30 秒用于禁用工具的最终回答；每个 Provider 请求使用 `min(DLR_AI_PROVIDER_TIMEOUT_SECONDS, 当前阶段剩余秒数)`，因此旧的单次 Provider 180 秒配置不能越过本次 Assist 截止点。工具仍保持单次 10 秒和串行执行。

调用指纹只在参数通过现有严格 schema 后生成，内容为 `tool_name + canonical JSON(validated_args)`；JSON 使用排序 key 和确定性编码。重复指纹若没有由后续成功结果推进出的不同 ID / 参数，直接产生 `duplicate` 停止原因，不再次访问工具。连续失败以每个 `ToolExecution.status=error` 计数，成功即清零，第三次连续失败停止。

选择该方案是因为所有边界都属于同一个请求生命周期，放在一个对象中可避免分散计数和异常路径遗漏。备选方案是只把两个常量改大；它不能解决超时、循环、轨迹丢失和强制最终回答，因此不采用。另一个备选是 `asyncio.wait_for` 包裹整个同步调用；当前 Provider / 知识源使用同步 I/O，取消不能可靠终止底层线程，按每次网络调用传递剩余 timeout 更可控。

### 2. 保护触发后只进行一次无工具最终化，并提供服务端安全降级

正常循环和保护性停止共用一个出口：

1. 将停止原因和“不得再调用工具、必须输出严格 `AiModelOutput`”的短控制消息追加到内存中的 Provider 消息；该消息只引用稳定 stop code 和已有工具消息，不复制 Prompt 或原始结果。
2. 以 `tools=None` 最多调用一次 Provider，timeout 不超过硬截止剩余时间。
3. 若得到合法 `AiModelOutput`，按现有 Candidate 配置不可变校验返回，并附带此前完成的所有脱敏 `tool_calls`。
4. 若 Provider 再次提出工具、超时、不可达或给出非法最终 JSON，则返回按系统语言生成的服务端安全消息、`candidate=null`、已完成工具摘要和既有 provider / model 标识。消息只说明停止原因、已取得多少成功结果和哪些部分未确认，不拼接完整工具结果。

预算、重复、连续失败、累计结果和总时限不再仅以 `ai_tool_limit_exceeded` 502 结束；真正发生在第一次 Provider 调用且没有任何可恢复上下文的 Provider 鉴权 / 配置错误仍沿用现有错误合同。这样既满足“强制最终回答”，又不把 Provider 不可用伪装成可靠答案。

备选方案是将 `executed_tools` 塞入错误响应；现有前端错误合同不会把它当成正常对话消息，用户仍无法得到基于已有证据的回答，因此不采用。

### 3. 以服务端知识库阶段机拒绝跳步，而非只增强 Prompt

当 `knowledge_search_enabled=false` 时，阶段为 `disabled`，现有可选工具路径不变。启用时阶段按以下状态推进：

```text
need_list
  -> list_empty / list_failed -> stopped
  -> need_search
       -> search_empty / search_failed -> stopped
       -> need_read
            -> read_success -> ready
            -> read_failed -> stopped
```

- `need_list` 只允许 `list_knowledge_bases`；非知识库工具、错误顺序或 Provider 直接 final 均不被接受。
- `need_search` 只接受使用本次列表真实返回 `knowledge_base_id` 的 `search_knowledge`。至少一次搜索满足强制合同；空结果进入可透明回答的 `stopped`。
- `need_read` 只接受使用本次搜索真实返回 `item_id` 的 `read_knowledge`。读取成功后为 `ready`；没有可读命中时不制造 read。
- 只有 `ready` 或带明确 empty / failure 原因的 `stopped` 才能接受最终内容。阶段未完成而 Provider 直接 final 时，系统追加一条短的阶段纠正消息并再次调用；这种不合规也计入连续失败保护，最多三次。
- 同一 Provider 轮次若提出多个调用，仍按现有串行顺序逐个校验和推进；后一个调用只能使用前一个实际返回的 ID，不能用模型虚构 ID 跳步。

Prompt 仍说明顺序以提高首轮成功率，但状态机才是权威门禁。最终化控制消息要求把失败 / 空结果与任何模型补充显式区分，并禁止引用本次工具结果中不存在的来源。

备选方案一是由 Control 自动对所有知识库执行搜索，既会快速耗尽 16 次预算，也无法可靠判断用户意图；不采用。备选方案二是为每种 Provider 增加强制 `tool_choice` 协议；当前适配层只有统一的 `tools_supported` 能力，不同协议支持度不一致，扩大兼容面且仍不能验证返回 ID，因此本轮采用协议无关的服务端阶段校验。

### 4. 审计使用专用 JSONL RotatingFileHandler，并与普通 logrotate 隔离

新增窄模块负责类型化事件构造和写入，文件固定为：

```text
<DLR_PLATFORM_LOG_ROOT>/control/ai-tool-audit.jsonl
```

使用 Python 标准库 `RotatingFileHandler`，默认 `maxBytes=10 MiB`、`backupCount=10`，新增两个正数且有上限的部署配置。扩展名使用 `.jsonl`，现有宿主机 `/control/*.log` 外部 logrotate 不会再次处理该文件，避免双重轮转。专用 logger 设置为 `INFO` 且关闭 propagation，不再受 Control root logger 的有效级别影响，也不把 JSON 事件复制到普通 `control.log`。

每个事件为一行完整 JSON，字段固定为：UTC 时间、schema version、`request_id`、`conversation_id`、`adapter_id`、round、call index、tool、status、duration_ms、result_size、result_truncated、error_code / stop_reason 和 `args_summary`。执行完成或拦截后立即 `emit`，不等待 Assist 成功；Python handler 每次 emit 后 flush，因此后续 Provider 失败不丢弃已写事件。另写 request terminal 事件记录最终状态和计数，但不包含回答正文。

审计 `args_summary` 不直接复用 UI 摘要：

- 已校验参数只保留工具专属白名单元数据；查询文本记录字符数和 SHA-256 短摘要，不记录原文；source / knowledge base / item ID 先执行现有按值和模式脱敏并限制长度。
- 未知工具或非法 JSON 只记录工具名、原始参数字节数和稳定错误码，不记录原始参数。
- 永不把 `result_summary` 或 `model_content` 交给审计模块；只记录安全结果字节数和截断布尔值。

Web 在面板会话建立时用 `crypto.randomUUID()` 创建内存态 `conversation_id`，该 Adapter 会话内的发送、重试和重新生成复用它；新挂载 / 新 Adapter 生成新值。`AiAssistRequest` 增加可选、格式受限的 `conversation_id`，旧客户端缺失时服务端生成 request-scoped fallback；每个服务端请求总是生成新的 UUID `request_id`。两者都只是关联元数据，不承担身份或授权。

备选方案是继续写普通 `control.log`。它依赖 root 日志级别且本地环境可能未安装外部 logrotate，正是当前轨迹缺失和潜在无界存储的根源，因此不采用。数据库审计表会引入 migration、清理 Job 和敏感数据治理复杂度，也超出“记录日志”的范围。

### 5. 截断继续由服务端判定，展示层只归一化为省略号

服务端保留单次 / 累计结果大小、`result_truncated` 和模型上下文中的结构化 `truncated` 事实；面向 UI 的新摘要后缀改为单个 `…`。Web 在 `AiToolCallSummary -> tool-call part` 转换处同时兼容新后缀和旧 `…[DLR 工具结果已截断]` 标记，移除内部标记后确保截断摘要只以一个 `…` 结尾。`DlrToolCallUI` 删除独立 `ai-tool-truncated` 文案和 live-region 内容，错误状态 / 错误码显示不变。

在转换层处理可使用响应中的 `result_truncated`，又能兼容旧后端返回，且不会放松服务端安全边界。只隐藏 CSS 文案的备选方案会让旧内部标记仍出现在正文和无障碍树中，因此不采用。

### 6. 历史日志按钮复用实时日志已经验证的颜色合同

保持 `.live-log-pane .log-toolbar, .history-log-pane .log-toolbar` 的深色表面，扩大当前按钮规则的作用域，使 `.log-pane .log-toolbar .ant-btn` 在普通和最大化布局都使用实时日志现有的前景、背景、边框、hover、focus-visible 和 disabled 值。搜索 Input 保持 Ant Design 白色输入表面，并补充必要的 prefix、边框和 focus 对比规则。组件 DOM、按钮事件、日志正文背景和实时日志控制逻辑不改。

这种最小 selector 修复能让历史日志获得与实时日志相同的语义状态，同时保持用户已验收的实时视觉值。把历史工具栏改回全白的备选方案会造成全屏 / 普通状态跳变，也与当前共用深色日志工作区不一致；仅把图标设为白色则遗漏 hover、focus 和 disabled 状态。

浏览器验收以 computed style 计算文字 / 图标 / focus 对比度，并在 `zh-CN`、`en` 下分别验证历史普通 / 全屏及实时底部 / 全屏 / 恢复；同时检查搜索、复制、下载、暂停 / 继续、最大化 / 恢复、键盘焦点、console、page error、失败请求和横向溢出。

## Risks / Trade-offs

- [8 轮 Provider 往返增加最坏成本和延迟] → 16 次、150 秒、30 秒最终化保留、重复和三连败共同形成硬边界；测试验证不得出现第 9 轮 / 第 17 次调用。
- [同步 I/O 在截止点附近仍有少量清理开销] → 每个网络请求都取剩余 timeout，使用单调时钟，并在硬截止后不再访问 Provider / 工具；验收以受控 fake clock / fake Provider 测量，而不声称毫秒级实时保证。
- [Provider 多次拒绝知识库工具导致用户得到降级说明] → 阶段纠正最多三次，之后透明停止；相比接受固有知识伪装成检索结果，这是有意的安全取舍。
- [审计参数摘要仍可能携带业务标识] → 仅白名单、查询 hash / 长度、既有按值和模式脱敏、严格长度限制；测试把 Secret 放入每条可疑路径并扫描所有轮转文件。
- [应用内轮转与多 Control 副本并发写同一文件不安全] → 当前 Compose 架构是单 Control 且每个部署使用本机 bind mount；本 change 不宣称共享存储或多进程集中审计。未来多副本需独立实例文件或集中日志设计。
- [CSS selector 扩大可能影响其他 LogView 嵌入点] → selector 限定在 `.log-pane .log-toolbar`，组件测试覆盖 history / live / maximized，浏览器同时回归两条路径。
- [降级回答不包含完整工具内容，信息量有限] → 这是防止错误路径泄露结果或绕过 Candidate Schema 的有意限制；正常最终化仍可使用内存中的已脱敏 Provider tool messages。

## Migration Plan

1. 先发布后端编排、可选 `conversation_id`、有界审计 handler 和默认配置；无需 Alembic migration，旧客户端请求继续有效。
2. 同一版本发布 Web，会话开始后发送 `conversation_id`，归一化截断摘要并修复日志工具栏；Control 与 Web 应作为同一 Compose 版本升级。
3. 在 `.env.example` 和平台日志部署文档记录 Assist 总时限、审计单文件大小 / 保留数量、文件位置和最大磁盘占用计算；确认 `platform-logs/control/` 可写。
4. 通过 backend、web、构建、隔离 fake Provider / knowledge source 及真实浏览器双语验证后再部署；不使用真实 Credential 作为测试数据。

回滚时同时回滚 Control 和 Web 镜像 / 工作树及新增环境变量；数据库无变化。`ai-tool-audit.jsonl*` 仅含脱敏日志，可由旧版本忽略并按部署保留策略删除，不需要数据回迁。不要只回滚后端而保留发送新字段的前端，因为旧严格请求 schema 会拒绝未知字段。
