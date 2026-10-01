## Context

见 `proposal.md`。当前 `services/ai.py` 的 `_assist_messages()` 同时生成长 system 文本和 Provider 消息；`AiAssistRequest` 只接收最近八条可见消息，`conversation_id` 仅作请求审计关联。`api/ai.py` 已取得认证 Principal 并检查 Adapter edit 权限，但尚未传给 service。现有 Tool Loop、Knowledge 状态机、附件和 Strict JSON 校验必须原位保留；现有数据库迁移已到 0044。

## Goals / Non-Goals

**Goals:** 以单一组装入口服务普通 Assist 和专用摘要；按每次实际 Provider payload 计预算；会话状态与 owner 由服务端控制；逐检查点提交和验证，最终一个 PR。

**Non-Goals:** 不移植 AI Provider、Tool Loop 或认证体系；不保存完整 Prompt/原始响应/附件正文；不新增框架、在线 Prompt 编辑、自动 Apply/Save/Run、跨 Adapter 记忆。

## Decisions

### 1. 静态资源与两次有界迁移

`dlr.control.ai.prompts` 只包含三个纯 UTF-8 Markdown，使用 `importlib.resources` 读取并在模块初始化验证。`PromptContext` 保存经服务端验证的请求事实，构造时防御性复制内部可变对象；`PromptBuildResult` 持有独立的消息值和非敏感 diagnostics。Wave A 把现有 `_assist_messages()` 收敛为 Builder 委托，先保持角色、顺序、条件和安全校验等价；Wave B 再把 Working Copy/材料移到当前 user 的 `DLR_REQUEST_CONTEXT_V1` 结构。机器 Schema、Runtime Contract 与 Tool 定义继续由代码生成。

选择两个代码检查点，是为了把结构回归与模型行为变化分开定位。替代方案是一次重写长 Prompt 和消息角色；若失败将无法判断是抽取还是行为造成。

### 2. 按调用目的复用传输和预算

定义普通 Assist、工具后续/收尾、内部摘要三个明确用途。Builder 根据用途选择必需节：摘要用独立 Pydantic 输出 Schema，不复用 `AiModelOutput`，无工具和不相关的代码合同。Provider 传输仍用现有适配层。每个 `Provider.generate` 入口前对最终 payload 运行同一预算函数，包括 `_finalize_after_tool_stop()` 的追加指令。预算按模型已知窗口或可配置保守默认窗口、实际 UTF-8 文本大小、角色/工具包装、图片预留、输出预留和误差余量估计；不将字符数冒充精确 tokenizer 计数。未知模型采用保守配置；验证相应 Provider 的计费/窗口后才细化估算。低优先级历史与参考材料按消息或材料边界缩减，工具 call/result 必须成对；必需内容保持完整或在调用前明确拒绝。

替代方案是在 HTTP 请求入口只做一次总长度检查；它漏掉工具结果、收尾追加规则和摘要，因此不采用。总时限从进入本轮 Assist 即确定为单一 monotonic deadline，所有 Provider/工具/摘要调用只用剩余时间，保留既有收尾预留。

### 3. 会话、状态与摘要的权威边界

新增可选 `session_id`，区别于现有仅用于审计的 `conversation_id`；未选择持久会话的旧客户端仍可使用原 API，但无跨刷新恢复。服务端分配 session ID、消息顺序和修订号。状态包含带来源的明确目标、约束、确认决策、推断、待办与未解问题；摘要包含来源范围和最后覆盖消息序号，不保存代码权威副本。Web 发送当前 Working Copy，每次请求服务端重新验证 Adapter 和会话，摘要作为低优先级参考加入。

持久会话将待提交的 user 消息和请求代次先写入事务，再在事务外调用 Provider；完成时以会话修订号和请求代次做 CAS，防止取消、并发和迟到响应覆盖较新状态。摘要仅处理已持久化、尚未覆盖的连续范围，按来源、长度和专用 Schema 校验后 CAS 更新；失败保持旧摘要与覆盖游标，必要时明确提示上下文未完整保留。真实摘要不要求每轮调用。

替代方案是只用浏览器 recent_messages 或把完整请求 JSON 入库；前者无法恢复早期约束，后者会持久化不必要的代码与敏感材料。

### 4. 受控存储和身份

用现有 PostgreSQL/SQLAlchemy/Alembic 增量新增 `ai_conversations` 与有界 `ai_conversation_messages`，含 adapter_id、owner_kind、account_user_id 或部署级 owner、revision、摘要及 cursor、过期时间；只保存用户/助手可见文本与选择的状态字段，不保存附件、工具大输出、Provider 原始响应、Credential 或完整 Working Copy。账号 owner 使用服务端 Principal 的稳定 user_id；superadmin Token 使用固定部署级 owner，不使用 Token 值或哈希。API 的 list/read/continue/clear/delete 每次均先检查 Adapter edit，再验证 owner。Web 在身份切换时清理显示状态。会话删除和期限清理只操作这些新行，不触碰业务历史、审计或现有卷。

替代方案是把 `user_id=None` 视作所有管理员共享；这会混淆 account 管理员与部署 Token，因而不采用。Token 共享空间的真实语义须在 UI/文档说明。

## Risks / Trade-offs

- [Prompt 拆分改变 Provider 行为] → Wave A 的确定性等价测试、Wave B 的固定用例和真实 Provider 首轮/有限重试分开记录；Fake Provider 不充当模型质量证明。
- [预算估算不等于 Provider tokenizer] → 保守窗口与预留可配置，记录估算方法；未知模型宁可在调用前拒绝，不截断必需内容。
- [并发导致状态回退] → 请求代次、修订号、摘要覆盖游标都在数据库事务中比较，Provider 调用不占用数据库锁。
- [恢复误用旧代码或临时材料] → 重新取当前 Working Copy；摘要不存代码真相，材料失效显式提示，工具不自动重放。
- [新增持久化扩大隐私面] → 仅 opt-in 会话、受限字段/期限、owner 与 Adapter 双重校验，测试账号/Token/撤权矩阵。

## Migration Plan

按 #128 Wave A、Wave B，再按 #151 预算、状态/摘要、持久化顺序在同一分支提交。数据库迁移只新增表与索引，已有 Adapter、Execution、审计、文件和卷保持原状；先在隔离 PostgreSQL 测升级及访问控制，最终在固定预览环境沿现有受限控制器升级。旧客户端不传 `session_id` 时继续原单轮行为。应用代码回退只在与新表兼容且经过现场核验时执行；不自动降级数据库、删除会话表或清空卷。最终记录合并 SHA、部署 SHA、真实 Provider 与浏览器验收范围。
