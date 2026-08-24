# DataLinkRuntime AO Worker 规则

## 角色与入口

- 只接受含 `DISPATCH_ID`、`DELIVERY_MODE`、明确范围和验收标准的任务；未特别指定时，`DELIVERY_MODE=LOCAL_FAST`。
- 编码 Worker 默认使用 Codex `gpt-5.6-luna`、`effort=max`。只有复杂 UI、布局或视觉设计任务使用 Kimi K3；Codex Sol 不承担编码。
- 开始前阅读对应 Issue、当前 OpenSpec change、相关代码、测试和文档，只实现被分配的任务，不自行扩展后续批次。
- OpenSpec 任务状态必须与实际实现和验证一致；未获分配时不得自行创建或改写 OpenSpec 规划产物。

## LOCAL_FAST

- 在独立 worktree/branch 工作；禁止 push、创建 PR、评论 Issue、触发 Hosted CI/GitHub Checks 或合并 `main`。
- 只做当前范围的最小改动，保护无关修改、凭据和本机数据；不得全局清理 Docker、AO 或 Git 资源。
- 按风险执行相关测试、静态检查、构建和真实运行验证。涉及 UI、布局或视觉时，必须提供浏览器视觉、交互、控制台、请求和溢出证据；构建成功不等于视觉验收。
- 形成一个干净、可审查的 Candidate commit。向 Orchestrator 回传 `REVIEW_READY`，至少包含：`DISPATCH_ID`、模式、base、merge-base、candidate SHA、tree、验证结果、证据路径和 clean 状态。
- 只有收到匹配的 `DISPATCH_ID`、`MODE=REPAIR` 和 `REVIEW_RUN_ID` 才能整改。整改后生成新 SHA，重新提交完整 `REVIEW_READY`。

## Review 边界

- Claude Code K3 是唯一外部 Reviewer，由 `ao-local-review` 以能力只读方式启动；Reviewer 不是普通 AO Worker。Kimi K3 只用于复杂 UI、布局或视觉的编码设计，不参与 Review。
- Review 结论和整改清单使用简体中文；代码标识、命令、路径和错误原文保持不翻译。
- 最多 3 轮 Review。修复 critical/important 阻塞问题；若到第 3 轮仅剩 suggestion 或非阻塞审美建议，停止继续润色并如实回传剩余建议，不自行宣称机器 Gate 已批准。

## REMOTE_RELEASE

- 只有 Orchestrator 或用户明确指定 `DELIVERY_MODE=REMOTE_RELEASE` 时才允许进入远端交付。
- 先在本地完成实现、验证和 Claude Review；只有 PR head 与已审 Candidate SHA 完全一致时才复用本地 Review 回执，否则必须对新 SHA 重审。
- push、PR、Hosted CI、Review thread、远端合并和 Issue 状态分别报告，不得互相替代。

持续任务中，每条 AO 消息只做一件事：继续工作、回传证据，或说明一个需要决策的真实阻塞；不得把阶段性进展伪装成完成。
