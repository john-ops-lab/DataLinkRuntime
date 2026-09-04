# DataLinkRuntime AO Worker 规则

## 角色与入口

- 只接受含 `DISPATCH_ID`、`DELIVERY_MODE`、明确范围和验收标准的任务；未特别指定时，`DELIVERY_MODE=LOCAL_FAST`。
- 编码 Worker 由 AO 项目的 Worker 配置直接使用 Codex `gpt-5.6-luna`，Codex 用户级配置统一使用 `model_reasoning_effort=max`；创建 Worker 时不得用会话参数覆盖这两个设置。只有复杂 UI、布局或视觉设计任务使用 Kimi K3；Codex Sol 不承担编码。
- 开始前阅读对应 Issue、当前 OpenSpec change、相关代码、测试和文档，并核对分配的 `INTEGRATION_BRANCH` 与精确 `INTEGRATION_HEAD`；Worker 分支必须以该 SHA 为基线，只实现被分配的任务，不自行扩展后续批次。
- OpenSpec 任务状态必须与实际实现和验证一致；未获分配时不得自行创建或改写 OpenSpec 规划产物。

## LOCAL_FAST

- 在从指定 `INTEGRATION_HEAD` 创建的独立 worktree/branch 工作；禁止 push、创建 PR、评论 Issue、触发 Hosted CI/GitHub Checks，或自行合并 `main`/集成分支。
- 只做当前范围的最小改动，保护无关修改、凭据和本机数据；不得全局清理 Docker、AO 或 Git 资源。
- 按风险执行相关测试、静态检查、构建和真实运行验证。涉及 UI、布局或视觉时，必须提供浏览器视觉、交互、控制台、请求和溢出证据；构建成功不等于视觉验收。
- 形成一个干净、可审查且可从 `INTEGRATION_HEAD` fast-forward 的 Candidate commit。向 Orchestrator 回传 `REVIEW_READY`，至少包含：`DISPATCH_ID`、模式、`INTEGRATION_BRANCH`、base、merge-base、candidate SHA、tree、验证结果、证据路径和 clean 状态。
- 只有收到匹配的 `DISPATCH_ID` 和 `MODE=REPAIR` 才能整改；AO 官方 Review 的整改必须来自 AO 记录并投递给该 Worker 的当前 PR 反馈。整改后生成新 SHA，重新提交完整 `REVIEW_READY`。

## Review 边界

- AO 官方 Reviewer 配置为 Claude Code harness；实际模型由本机 Claude Code 官方配置解析。Reviewer 由 AO 官方 Review adapter 以只读工具策略启动，不是普通 AO Worker。
- AO Review 只适用于已关联开放、非草稿 PR 的 Worker，并绑定当前 PR head SHA。`LOCAL_FAST` 不创建 PR，因此不产生 AO Review 回执，也不得宣称机器 Review 已通过。
- Review 结论和整改清单使用简体中文；代码标识、命令、路径和错误原文保持不翻译。
- 收到 AO 官方 Review findings 后，只有匹配当前 PR head SHA 的结论有效；任何修复产生新 SHA，必须等待该新 head 的 Review 状态重新收敛。

## REMOTE_RELEASE

- 只有 Orchestrator 或用户明确指定 `DELIVERY_MODE=REMOTE_RELEASE` 时才允许进入远端交付。
- 先在本地完成实现和验证，再创建或更新 PR；只有 AO 官方 Review 的 target SHA 与 PR head 完全一致时，该 Review 回执才有效。
- push、PR、Hosted CI、Review thread、远端合并和 Issue 状态分别报告，不得互相替代。

持续任务中，每条 AO 消息只做一件事：继续工作、回传证据，或说明一个需要决策的真实阻塞；不得把阶段性进展伪装成完成。
