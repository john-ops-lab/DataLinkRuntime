## 1. AI 工具编排与截止时间

- [x] 1.1 将工具预算调整为 8 轮 / 16 次，并新增默认 150 秒、仅允许 120–180 秒的 Assist 总时限配置及 Provider 剩余时间 timeout 传递；以配置边界测试和 Provider timeout 单元测试验证旧的单次 180 秒 timeout 不能越过 Assist 截止点。
- [x] 1.2 实现请求级编排状态、规范化调用指纹、重复拦截和成功重置 / 三次连续失败保护；以 `backend/tests/test_ai_tools.py` 和 `backend/tests/test_ai.py` 覆盖语义等价参数、由新结果推进的不同参数、三连败及成功重置。
- [x] 1.3 把轮次、调用数、累计结果和总时限触顶改为至多一次禁用工具最终化，并实现 Provider 再次调用工具 / 超时 / 非法 JSON 时的 `candidate=null` 双语安全降级；测试断言不执行第 9 轮或第 17 次调用、正常返回保留全部已完成摘要且预算触顶不再单独返回 502。
- [x] 1.4 回归工具白名单、只读限制、参数 / 单次 timeout / 结果大小 / Secret 脱敏及 Candidate 配置不可变合同；运行相关安全测试并断言扩容后未知或写入型工具仍使用稳定错误码拒绝。

## 2. 知识库强制优先状态机

- [x] 2.1 在启用 `knowledge_search_enabled` 时实现 `need_list → need_search → need_read → ready/stopped` 服务端阶段机，校验 knowledge base / item ID 必须来自本次先前结果；以 fake Provider 单元测试证明首轮直接答案、跳步、伪造 ID 和非知识库工具均不能绕过门禁。
- [x] 2.2 实现空列表、空搜索、列表 / 搜索 / 读取失败和阶段纠正三连败的透明最终化文案，并要求知识库事实与模型补充分区；以 `backend/tests/test_ai_knowledge.py` 覆盖正常 list/search/read、各失败阶段、部分结果和来源不得伪造。
- [x] 2.3 保持未勾选知识库时的现有可选工具请求字节 / 行为兼容，并验证知识库强制流程复用重复、预算和总时限保护；运行现有 AI / knowledge 回归测试确认未启用请求不会新增知识库调用。

## 3. 脱敏工具审计与有界轮转

- [x] 3.1 为 `AiAssistRequest` 增加可选、格式受限的 `conversation_id`，每次服务端 Assist 生成唯一 `request_id` 并为旧客户端补 request-scoped 会话标识；以 schema / API 测试验证同会话不同请求可关联且旧 payload 继续通过。
- [x] 3.2 新增专用 JSON Lines 审计模块和 `control/ai-tool-audit.jsonl` 应用内 `RotatingFileHandler`，默认 10 MiB、10 个历史文件，配置只能取有界正数且不传播到普通 root logger；以临时目录和极小测试阈值验证完整行轮转、删除最旧文件、重启续写及不会与 `*.log` 外部轮转重叠。
- [x] 3.3 在成功、工具失败、Provider 后续失败、重复 / 预算 / 连败 / 时限拦截和请求终止路径逐条立即写入带 request / conversation / round / call 关联的白名单事件；以中途抛错测试直接读取文件，证明先前事件已 flush 且计数和稳定 stop code 正确。
- [x] 3.4 为审计参数实现工具专属白名单摘要，查询只保留长度与 hash，非法参数只保留大小和错误码，且审计 API 不接受 result / Prompt / attachment / source code / reasoning / raw response 字段；向所有路径注入占位 Secret、Token、Cookie 和敏感正文后扫描当前及轮转文件，验证原值和完整内容均不存在。
- [x] 3.5 更新 `.env.example`、`docs/deployment/platform-logs.md` 及必要的 README 配置说明，记录总时限、审计文件位置、大小 / 数量上限、最坏磁盘占用和回滚方式；运行项目文档链接 / 配置测试验证示例无真实凭据且中英文文档合同未被破坏。

## 4. Web 会话关联与工具截断呈现

- [x] 4.1 在 `AiAssistantPanel` 会话内生成并复用内存态 `crypto.randomUUID()`，随发送、重试和重新生成提交 `conversation_id`，Adapter 切换 / 组件新挂载时更新且不写 localStorage / sessionStorage；以 Vitest 检查同会话复用、跨会话变化和请求体不含其他持久化会话数据。
- [x] 4.2 将新旧工具截断后缀统一为单个 `…`，删除 `DlrToolCallUI` 的额外截断文案 / live-region 提示，同时保留 `result_truncated`、服务端大小限制和错误码展示；更新 zh-CN / en key 与组件测试，验证页面及无障碍树均无“已截断 / truncated”提示且未截断结果不追加省略号。

## 5. 历史与实时日志工具栏回归修复

- [x] 5.1 将实时日志已验证的按钮前景、背景、边框、hover、focus-visible 和 disabled 规则窄化复用于 `.log-pane` 下的历史普通 / 最大化工具栏，并补齐搜索 Input 对比度；用组件测试断言 history / live DOM、可访问名称和所有按钮行为不变。
- [x] 5.2 扩充 `OutputView` / `LiveLogWorkspace` 测试，覆盖历史搜索、复制、下载、最大化 / 恢复及实时暂停 / 继续、底部 / 全屏 / 恢复；测试必须证明历史修复不会改变实时日志内容、跟随状态或现有深色正文效果。

## 6. 集成、静态检查与浏览器验收

- [x] 6.1 运行 `cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`，记录实际结果并修复本 change 引入的失败；任何环境或既有失败必须单独标注，不能以局部通过代替全量结果。
- [x] 6.2 运行 `cd web && npm run lint && npm run typecheck && npm run test && npm run build`，确认双语 key、工具卡片、日志组件及生产构建全部通过且没有降低既有断言。
- [x] 6.3 扩展并运行隔离 fake Provider / fake knowledge source 的 `./scripts/compose-smoke.sh`，验证 8/16 边界、强制 list/search/read、失败后最终回答、审计 JSONL 落盘与轮转；只使用占位凭据，并扫描 Compose 输出和审计文件确认无 Secret / Prompt / 原始响应。
- [x] 6.4 使用真实浏览器在 `zh-CN` 和 `en` 分别验收历史日志普通 / 全屏 / 恢复与实时日志底部 / 全屏 / 恢复，逐项操作搜索、复制、下载、暂停 / 继续和键盘焦点；保存截图及 computed-style 对比度证据（文字至少 4.5:1、图标 / focus 至少 3:1），并断言无 console error、page error、失败请求或水平溢出。
- [x] 6.5 使用真实浏览器勾选知识库检索并完成成功、空结果和失败三条 fixture 对话，验证工具顺序、来源边界、模型补充标识、截断仅显示 `…` 和同会话关联；归档网络响应与脱敏审计关联证据，不把 Prompt、附件正文或 Credential 写入验收产物。
