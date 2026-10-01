## 1. #128 Wave A：结构等价迁移

- [x] 1.1 建立并验证三个静态 Prompt package resources、启动期非空/UTF-8 检查及 revision/非敏感诊断；用资源缺失、不同 cwd 的定向测试验证。
- [x] 1.2 实现 typed 请求级 PromptContext 与深层防御性快照，覆盖五语言、base version、Secret 名称、Snippet、附件/图片和 saved_managed_input；用嵌套对象修改与敏感诊断反例测试验证。
- [x] 1.3 建立唯一 Prompt Builder 并让 `_assist_messages()` 委托它，先保持当前 Provider 消息布局及 Strict JSON/Tool/Knowledge 行为等价；用现有 AI 测试和专用 Builder 矩阵验证。
- [x] 1.4 验证 Markdown 出现在 wheel、sdist 与 container 产物，缺失/坏资源在接受请求前失败，并跑适用 Backend 静态检查；保留产物检查结果。

## 2. #128 Wave B：消息分层与行为

- [ ] 2.1 把 Working Copy 和本轮材料移至当前 user 的结构化数据，保持历史顺序、native image parts 与工具消息配对；用角色/边界定向测试验证 system 不再携带动态代码。
- [ ] 2.2 按工具和知识能力、检索开关及实际材料选择静态规则，精简普通回答/Candidate/诊断规则，保留五语言、Managed Input 与服务端硬校验；用知识状态机和输入矩阵验证。
- [ ] 2.3 对固定场景运行相关 Backend/Web 回归、静态检查及真实 Provider Smoke Test，分别记录首轮与有界重试、Java/Go 字面要求和 Candidate 应用边界；不把 Fake Provider 结果标为模型质量通过。

## 3. #151：整体预算

- [ ] 3.1 实现按调用目的和模型/保守默认窗口的统一预算器，包含工具定义、图片、输出与估算余量；用小窗口、未知模型、必需内容超限及材料裁剪测试验证。
- [ ] 3.2 将预算检查放在初始、工具后续和 `_finalize_after_tool_stop()` 每次实际 Provider 调用前，并共用单一 Assist deadline；用工具配对、大附件与收尾超限测试验证无越界调用。
- [ ] 3.3 增加无工具的专用摘要输入与结构化输出合同，复用 Provider/预算层而不解析为 Candidate；用非法工具调用、缺少来源和摘要超限测试验证拒绝。

## 4. #151：任务状态与滚动摘要

- [ ] 4.1 定义带来源、确认级别、撤销与修订号的 ConversationState；用早期约束、后续撤销和 Working Copy 权威性测试验证。
- [ ] 4.2 保留待压缩消息，按连续范围、来源版本和覆盖游标增量摘要；失败、取消或超时保持有效旧摘要，已覆盖回复变更时使依赖摘要失效并重算；用确定性游标、旧回复替换及摘要失败测试验证。
- [ ] 4.3 将稳定 turn_id、同键请求摘要、重新生成目标与请求代次/状态修订 CAS 接入 Assist 流程；用响应丢失后重试、同键冲突、已有回复重新生成、并发及迟到响应反例验证不重复用户记录或覆盖新状态。
- [ ] 4.4 完成至少一组真实 Provider 长对话验收，核早期约束、撤销、Candidate 与应用后的当前代码，并记录请求版本、游标与首轮/重试边界。

## 5. #151：持久化与恢复

- [ ] 5.1 新增受限会话表、消息表及 Alembic 增量迁移，明确期限、字段上限和删除语义；用真实 PostgreSQL 升级与字段检查验证旧业务数据不变。
- [ ] 5.2 传递认证 Principal，完成 account/部署级 superadmin owner 与会话 create/list/read/continue/clear/delete API；用两个账号、Token 轮换、权限撤销和伪造 ID 的真实数据库/API 测试验证。
- [ ] 5.3 在 AI 面板接入选定会话、刷新恢复、当前 Working Copy 重取及身份切换清理；先按项目 Ant Design skill 核版本快照，再用 Web 测试和真实 Chrome 交互验证。
- [ ] 5.4 验证服务重启、临时材料失效、代码变化、会话过期/删除及不重放工具；用实际数据库和浏览器结果核对恢复范围，并同步产品/架构文档。

## 6. 第四组交付与统一人工审查

- [ ] 6.1 最终 HEAD 运行适用 Backend/Web/迁移/Compose 门禁，做一次集中独立源码与数据安全审查，修复实质问题并核对证据绑定。
- [ ] 6.2 用唯一第四组 PR 完成当前 HEAD CI、授权合并及固定预览的同卷部署与实际 SHA/关键运行验证；原失败记录保留，不重跑已通过的第三组验收。
- [ ] 6.3 汇总 #128/#151 及前几组待用户确认项，提交第四组与第三组的统一人工外部审查材料；用户完成最终验收前，相关 Issue 保持 OPEN。
