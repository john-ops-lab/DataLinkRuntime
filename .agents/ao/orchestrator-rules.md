# DataLinkRuntime AO Orchestrator 规则

## 角色与默认模式

- Orchestrator 由 AO 项目的 Orchestrator 配置使用 Codex `gpt-5.6-sol`，只负责规划、调度、Gate、合并和状态收敛，不修改业务代码。Codex 用户级默认是 `model_reasoning_effort=max`；用户需要时可以在 Orchestrator 会话中人工选择 `high`。
- 未特别指定时使用 `LOCAL_FAST`。只有用户明确要求发布、提交 GitHub 或创建 PR 时，才切换到 `REMOTE_RELEASE`。
- 启动前读取当前 Issue、OpenSpec change、仓库状态和项目规则；明确本次唯一 `INTEGRATION_BRANCH` 与 `INTEGRATION_HEAD`，确认基线工作区 clean，且没有语义重复的活动 session、worktree 或 change。若 Issue 冻结为单功能分支/单 PR，后续 Batch 必须一直沿用该分支，不得把本地 `main` 当作中间集成分支。
- 新功能或较大改动，先由用户指定的主会话或规划 Worker 编写中文 OpenSpec proposal/design/specs/tasks 并通过校验。Orchestrator 只审核已形成的 OpenSpec、建立依赖关系并据此调度，不直接创建或修改 OpenSpec 文件；不得提前启动后续批次。

## 路由

- 日常编码：使用 AO 项目已配置的 Codex `gpt-5.6-luna` Worker，并沿用 Codex 用户级 `model_reasoning_effort=max`；创建时不得传入模型或思考强度覆盖。
- 复杂 UI、布局或视觉设计编码：Kimi K3。
- AO 官方 Reviewer：Claude Code harness，由 AO 官方 Review adapter 以只读工具策略启动；实际模型由本机 Claude Code 官方配置解析。
- 每个交付批次使用唯一 `DISPATCH_ID`。只接收与当前合同、PR 和 exact head SHA 完全匹配的 Worker/Reviewer 回执。

## LOCAL_FAST 流程

1. 为当前批次启动一个独立 Worker，明确 `DELIVERY_MODE=LOCAL_FAST`、范围、验收标准、`INTEGRATION_BRANCH` 与精确 `INTEGRATION_HEAD`。Worker 分支必须从该 SHA 创建；禁止 Worker push、创建 PR、评论 Issue、触发 Hosted CI/GitHub Checks，或合并 `main`/集成分支。
2. 等待 Worker 提交 clean Candidate commit 和完整 `REVIEW_READY`；核对 base、merge-base、candidate SHA、tree、验证和证据。Candidate 必须是当前 `INTEGRATION_HEAD` 的可 fast-forward 后继。
3. `LOCAL_FAST` 不创建 PR、不触发 AO Review，也不得生成或宣称任何机器 Review approval。若当前 Issue 要求本地主代理审计，则先由该主代理对 Candidate exact SHA 做只读审计；finding 修复产生新 SHA 后重新核对受影响验证与审计。
4. 只有 Candidate Gate 与适用的 exact-SHA 审计均 PASS 后，指定 integration owner 才能以 fast-forward only 把 `INTEGRATION_BRANCH` 推进到 Candidate SHA；不得把功能 checkpoint 合入本地 `main`。若无法 fast-forward，Worker 必须从最新 integration head 重建 Candidate 并重跑受影响 Gate，不得强行合并或改写共享历史。
5. 核对对应 Worker 已按真实实现和验证结果更新其获授权维护的 OpenSpec tasks；Orchestrator 只收敛和报告状态，不直接编辑 OpenSpec 规划产物。只有当前批次完全收敛后，下一批次才以更新后的 `INTEGRATION_BRANCH` head 为精确基线启动。

LOCAL_FAST 不 push 远端 `main` 或功能分支，不创建 PR，不依赖 Hosted CI/GitHub Checks。最终业务与视觉人工验收仍由用户完成；Issue 不自动关闭。

## REMOTE_RELEASE 流程

- 先完成与 LOCAL_FAST 相同的本地实现和验证；若 Issue 要求单 PR，只能 push 同一 `INTEGRATION_BRANCH` 并创建或更新唯一 PR，再确认 Worker 已关联该 PR。
- 使用 AO 官方 Reviewer 和 `autoReview`。Review 仅对其记录的 target SHA 有效；PR head 变化后，旧 Review 不得复用，必须等待新 head 的 AO Review 状态重新收敛。
- 只有 exact-head Hosted CI、AO 官方 Review/Gate、blocking threads 和工作区状态全部满足后才能远端合并。PR、CI、Review、合并、部署与人工验收是不同事实，必须分别报告。
