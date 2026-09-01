# Issue #130 实施前门禁回执

记录时间：2026-09-01 23:01:01 +0800。

## Git 与 GitHub 基线

- `origin/main`：`672a6811ff64fd6c61e22b858470857f67083fbe`。
- 唯一集成分支：`codex/issue130-reliable-runtime`。
- AO rules 治理 checkpoint：`e1178c45efff4b5c732d93bf690f17a3186051da`。
- 当前分支与 `origin/main` 的 merge-base 为上述 `origin/main` SHA。
- GitHub open PR 数量为 0；Issue #130 为 `OPEN`，Issue 正文中的 Batch 0 状态为 `FROZEN`。
- 实施前只保留本次 Batch 0 的 OpenSpec 新文件；两份既有 AO rules 修改已经审计、修正并单独提交，没有来源不明的工作区变化。

## OpenSpec 与串行交付合同

- Change：`issue130-reliable-execution-runtime`，schema：`spec-driven`。
- proposal、13 份 delta specs、design、tasks 与适用 `AGENTS.md`/AO rules 已完整读取。
- Batch 1、2、3 必须基于同一 `codex/issue130-reliable-runtime` 分支串行推进；每个 Worker 从上一 checkpoint 的精确 SHA 创建独立 Candidate。
- `LOCAL_FAST` 只形成 checkpoint、相关测试/故障证据和当前主代理 Sol exact-SHA 只读审计，不创建 PR、不触发或声称 AO 官方 Review，也不把 checkpoint 合入 `main`。
- 全部本地 Gate 通过后只 push 该功能分支并创建一个非 Draft PR；Hosted CI 与 AO 官方 Claude Review 必须同时绑定最终 PR head。

## AO 运行态

- AO daemon 为 `ready`，`ao doctor --json` 为 0 failures。
- Codex、Claude Code 均为 authorized；项目 Worker 配置为 Codex `gpt-5.6-luna`，Orchestrator 为 `gpt-5.6-sol`，Codex 用户级 `model_reasoning_effort=max`。
- 项目 `autoReview=true`，唯一 AO Reviewer harness 为 `claude-code`。
- 安装后遗留的 idle Orchestrator worktree clean、无独有 commit；旧会话已终止但未清理，避免其继续使用修正前规则。
- 当前 shell 的 `ao` 未加入 `PATH`；本次使用应用内置 CLI，AO daemon 启动的 session 会固定同一 CLI 路径，不影响 Worker hooks。

## 状态边界

- 本回执只证明实施前 Git/OpenSpec/AO 基线与串行合同。
- Pika/JCS/RabbitMQ 依赖 Gate、产品实现、Batch Gate、Hosted CI、AO 官方 Review、合并、发布和用户验收均尚未完成。
