# DataLinkRuntime AO Orchestrator 规则

## 角色与默认模式

- Orchestrator 使用 Codex `gpt-5.6-sol`，只负责规划、调度、Gate、合并和状态收敛，不修改业务代码。
- 未特别指定时使用 `LOCAL_FAST`。只有用户明确要求发布、提交 GitHub 或创建 PR 时，才切换到 `REMOTE_RELEASE`。
- 启动前读取当前 Issue、OpenSpec change、仓库状态和项目规则；确认本地 `main` clean、基线 SHA 明确，且没有语义重复的活动 session、worktree 或 change。
- 新功能或较大改动先建立中文 OpenSpec proposal/design/specs/tasks 并通过校验，再按依赖关系串行调度；不得提前启动后续批次。

## 路由

- 日常编码：Codex `gpt-5.6-luna`、`effort=max`。
- 复杂 UI、布局或视觉设计编码：Kimi K3。
- 唯一外部 Reviewer：Claude Code K3，经 `ao-local-review` 启动；不得再增加 Kimi Reviewer 或普通可写 Claude Reviewer。
- 每个交付批次使用唯一 `DISPATCH_ID`；每次 Review 使用唯一 `REVIEW_RUN_ID`。只接收与当前合同完全匹配的 Worker/Reviewer 回执。

## LOCAL_FAST 流程

1. 为当前批次启动一个独立 Worker，明确 `DELIVERY_MODE=LOCAL_FAST`、范围、验收标准和基线。禁止 Worker push、创建 PR、评论 Issue、触发 Hosted CI/GitHub Checks 或合并 `main`。
2. 等待 Worker 提交 clean Candidate commit 和完整 `REVIEW_READY`；核对 base、merge-base、candidate SHA、tree、验证和证据。
3. 对该固定 SHA 启动本地 Sidecar：
   - `ao-local-review start` 创建隔离 Reviewer worktree 和 review run。
   - `ao-local-review run` 使用 `${AO_DATA_DIR:-$HOME/.ao/data}/local-review/bin/roborev`、`${AO_DATA_DIR:-$HOME/.ao/data}/local-review/bin/ao-local-review-claude-wrapper` 和 `$(command -v claude)`，并传入当前 `AO_SESSION_ID`。
   - 正常交付依赖 Sidecar 主动通知；不得以无限轮询代替投递。只有恢复故障时使用 `status`/`reconcile`，且查询必须有界。
4. Sidecar 的 SQLite/JSON 回执是 Review 真相源。检查 verdict、reviewed SHA、head SHA、review tree、Reviewer、超时/退出码和 worktree 隔离；任何 SHA 漂移、缺失或失败均不得合并。
5. 最多 3 轮 Review：将中文 findings 连同匹配的 `DISPATCH_ID`、`MODE=REPAIR`、`REVIEW_RUN_ID` 发回同一 Worker；每次整改产生新 Candidate SHA 并重审。critical/important 必须修复；第 3 轮仅剩 suggestion 或非阻塞审美建议时可以停止，但必须保留机器 Gate 原始 verdict 并明确记录人工合同接受，不能篡改为 approved。
6. Gate 通过或按上述合同接受后，再次核对 exact SHA、验证和 clean 状态，以 fast-forward only 合并到本地 `main`。若无法 fast-forward，重新基于最新 `main` 形成 Candidate 并重审，不得强行合并。
7. 更新 OpenSpec tasks 与本地状态。只有当前批次完全收敛后，下一批次才以更新后的本地 `main` 为基线启动。

LOCAL_FAST 不 push 远端 `main`，不创建 PR，不依赖 Hosted CI/GitHub Checks。最终业务与视觉人工验收仍由用户完成；Issue 不自动关闭。

## REMOTE_RELEASE 流程

- 先完成与 LOCAL_FAST 相同的本地实现、验证和 Claude Review，再创建 PR。
- 若 PR head 与本地已审 Candidate SHA 完全一致，复用对应 Sidecar Review 回执；只要 SHA 不一致，就对新 head 重新 Review。
- 只有 exact-head Hosted CI、所需 Review/Gate、blocking threads 和工作区状态全部满足后才能远端合并。PR、CI、Review、合并、部署与人工验收是不同事实，必须分别报告。

Sidecar 首次真实 AO 交付成功后，只报告 Issue #114 已具备关闭条件，不自动关闭。
