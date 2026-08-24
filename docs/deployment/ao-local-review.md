# AO LOCAL_FAST 外部 Review Sidecar

`ao-local-review` 为 AO 的 `LOCAL_FAST` 模式提供本地外部 Review Gate。它不修改
AO daemon、AO SQLite 或 Worker Worktree，也不把本地 Review 冒充成 AO Native
PR Review。

## 角色边界

- AO 负责调度 Worker、接收 `LOCAL_REVIEW_RESULT`、返修和合并。
- RoboRev `v0.66.0` 只负责 Review job、Claude Code 调用、原始 SQLite 持久化和
  `review.completed` 事件。
- Sidecar 固定 Candidate SHA/tree、强制 Claude Review Profile、校验 RoboRev
  回执、生成结构化 Gate、恢复通知和归档。
- Claude Code / K3 是唯一外部 Reviewer。Kimi K3 只可作为复杂视觉 Coding
  Worker，不参与 Review。

## 安装

```bash
./scripts/install-ao-local-review.sh
```

安装器固定 RoboRev `v0.66.0` 并校验官方 Release SHA-256；不会安装 RoboRev
Git hook、Agent hook、skills，也不会启用 `fix/refine`。默认安装位置：

```text
~/.ao/data/local-review/
├── bin/
├── roborev/
└── reviewer-worktrees/
```

命令链接为 `~/.local/bin/ao-local-review`。

## 基本流程

Worker 必须先形成 clean Candidate commit，并把验证回执、浏览器证据写到 Worker
源码目录之外。JSON 证据若包含 `candidate_sha`，Sidecar 会校验它必须与当前
Candidate 一致。

```bash
ao-local-review \
  --repo "$WORKER_WORKTREE" \
  --worker-worktree "$WORKER_WORKTREE" \
  --json \
  start \
  --dispatch-id "$DISPATCH_ID" \
  --worker-session-id "$WORKER_SESSION_ID" \
  --base main \
  --candidate HEAD \
  --validation-file "$VALIDATION_JSON" \
  --browser-evidence-file "$BROWSER_EVIDENCE_JSON"

ao-local-review \
  --repo "$WORKER_WORKTREE" \
  --worker-worktree "$WORKER_WORKTREE" \
  --json \
  run \
  --roborev-bin "$HOME/.ao/data/local-review/bin/roborev" \
  --claude-wrapper "$HOME/.ao/data/local-review/bin/ao-local-review-claude-wrapper" \
  --real-claude "$(command -v claude)" \
  --ao-session "$ORCHESTRATOR_SESSION_ID"
```

Reviewer 的最终工具集合被包装器强制为 `Read/Glob/Grep`，并同时启用
`dontAsk`、`safe-mode`、空 strict MCP、禁用 Chrome 和 slash commands。包装器
保存实际 argv 与 Claude init event；仅凭 Prompt 或 RoboRev `--allowedTools`
不能通过 Gate。

## 状态与恢复

```bash
ao-local-review --repo "$WORKER_WORKTREE" --json status
ao-local-review --repo "$WORKER_WORKTREE" --json gate
ao-local-review --repo "$WORKER_WORKTREE" --json reconcile \
  --ao-session "$ORCHESTRATOR_SESSION_ID"
```

Gate 和 `delivery=pending` 先原子落盘，再执行 `ao send`。通知失败时运行
`reconcile` 只重投已持久化结果，不重新调用 Claude；连续三次失败后返回稳定错误。
RoboRev 的数据库、配置和运行目录按 Candidate/round 隔离；daemon 重启不会捞起已
supersede 的旧 job，也不会让旧任务回退到其他 checkout。创建新 round 时，Sidecar
仅清理由自身 owner receipt 标记的旧 Reviewer worktree 和旧 RoboRev round；成功
生成权威 JSON 回执后也会删除该 round 的临时 SQLite 与日志，避免长期累积。

Candidate 变化后必须创建新 round：

```bash
ao-local-review --repo "$WORKER_WORKTREE" --json supersede \
  --dispatch-id "$DISPATCH_ID" \
  --worker-session-id "$WORKER_SESSION_ID" \
  --base main \
  --candidate HEAD
```

合并前 `gate` 会实时校验 Worker HEAD、tree、clean 与当前 approved Gate。清理 Worker
Worktree 前归档：

```bash
ao-local-review --repo "$WORKER_WORKTREE" --json archive
```

权威 Sidecar 回执位于 `git rev-parse --git-path ao-local-review`；最终批准回执归档到
`~/.ao/data/local-review-archive/`。它们不进入产品 Git 历史。
