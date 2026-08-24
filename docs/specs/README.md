# DLR 历史 Specs 使用说明与现行性索引

`docs/specs/` 保留了 DLR 各历史里程碑的详细设计和关键决策记录。**截至 `v0.1.0`，本目录没有正在执行的 Target Spec；目录内文件均不得单独视为当前产品合同。**

在 M5.4 之后，DLR 的用户侧产品模型发生过明显收敛：早期围绕 `Publish / Published Version / Production Version / Production Worker / Start / Stop` 的设计已不再是当前用户流程，但相关文档仍保留作为实现历史和底层决策参考。

## 1. 阅读优先级

开发、Review 或 AI 编程工具遇到文档冲突时，按下面顺序判断：

1. **当前明确授权执行的 GitHub Issue / 里程碑合同（如有）**：定义本轮要实现的目标行为；不能从历史 Spec 自动推导新任务。
2. **最新 `main` 的代码、API Schema、migration 与自动化测试**：定义当前已经存在的实际行为和兼容边界。
3. **当前产品与架构文档**：`docs/zh-CN/product.md`、`docs/zh-CN/architecture.md` 及对应英文版。
4. **明确标记为 Current/Target 的阶段 Spec（如有）**：用于沉淀本阶段稳定的技术合同；若与当前阶段 GitHub Issue 冲突，以 Issue 为准。
5. **历史 Spec**：用于理解演进背景、底层约束和已实现细节，不能覆盖后续里程碑已经明确替代的产品语义。

简化为：

```text
当前获授权 Issue（目标，如有）
+ 最新 main（现状）
→ 当前 product / architecture
→ Current/Target Spec（如有）
→ 历史 Specs
```

## 2. 当前文件状态

| 文件 | 状态 | 使用方式 |
|---|---|---|
| `m1-adapter-management.md` | Historical / 部分已替代 | Adapter、不可变版本等早期底层设计可参考；Publish、历史 Version 用户流程不再作为当前产品合同。 |
| `m2-execution-loop.md` | Historical / 部分仍有效 | Control/Worker 分工、Execution 绑定不可变版本、子进程执行等底层思想仍有价值；历史 Version 手工执行等产品语义需以当前 main/M5.4+ 为准。 |
| `m3-observability-ux.md` | Historical / UI 流程已替代 | SSE、最终 result 权威等底层合同可参考；“编辑/测试运行/版本选择/Publish”旧工作台流程已被后续 M5 收敛。 |
| `m3-1-console-design-convergence.md` | Historical / 视觉基线 | App Shell、Workbench、高信息密度等设计原则可参考；具体 Version/Published UI 已过时。 |
| `m3-2-adapter-production-lifecycle.md` | **Superseded by M5.4** | 记录早期 Production 生命周期实现历史；不得用于推导当前用户侧 Publish/Production/Start/Stop 流程。 |
| `m3-3-multilang-runtime.md` | Partially current | Python/JavaScript/Java Runtime Contract、依赖格式和 Worker capability 等仍重要；继承自 M3.2 的 Production 生命周期语义已被替代。 |
| `m4-ai-editor.md` | Historical / 部分仍有效 | Working Copy、Candidate、Secret、Context Snippets、Provider 安全边界仍有参考价值；旧生命周期文字和“禁止 Tool Call”已被后续实现替代。 |
| `m5-7-ai-assistant.md` | **Implemented / 后续已演进** | 记录 M5.7 / Issue #80 当时的技术合同；相关能力随后由 M5.8～M5.11 继续演进，当前行为以最新 main 和现行文档为准。 |

## 3. 已明确失效的旧用户侧术语

下面这些词仍可能出现在 M1～M4 历史文档、旧 migration 或测试历史中，但**不得据此恢复旧产品流程**：

```text
Publish / Unpublish
Published Version
Production Version
Production Worker
强制 Save → Test → Publish → Start
历史 Version 作为普通用户主要操作对象
```

当前产品模型以 M5.4 之后的 `Task / Webhook + 保存 + 运行设置 + Execution/日志` 为准；系统内部仍可保留 Revision/历史字段作为审计或兼容事实。

## 4. AI Editor 的现行核心边界

后续开发应继续保持已经落入当前实现的核心边界：

- 当前 Working Copy 是本轮 AI 请求的唯一权威代码快照；
- Candidate 是完整 Snapshot，不是模糊 patch；
- Candidate 必须经过 DLR 本地严格 Schema Validation；
- Apply 只修改浏览器 Working Copy，不自动 Save / Test / Run；
- Secret 真值不得进入 Prompt、浏览器、Tool 展示或普通日志；
- `recent_messages` 有界，Context Snippets 是管理员显式冻结的本轮上下文；
- Adapter switch / late response 不得串线；
- hidden reasoning 不返回浏览器、不持久化、不进入下一轮。

M5.7 已引入受控只读 Tool Call，因此 M4 System Prompt 中“禁止 tool call”的旧限制已失效；具体行为以最新 main、测试和现行产品/架构文档为准。

## 5. AI 编程工具执行前必须做什么

Qoder、Codex、OpenCode 等工具在实施任务前应：

1. 先读本文件；
2. 再读当前明确授权执行的 GitHub Issue（如有）；
3. 检查最新 `main` 的真实代码和测试；
4. 阅读当前 `product / architecture`；
5. 只把相关历史 Spec 当作背景和局部合同来源；
6. 若历史 Spec 与后续 Issue/main 冲突，不得擅自恢复旧设计。

## 6. 维护规则

- 历史 Spec 默认保留，不因产品演进删除，以便追溯为什么曾经这样设计。
- 后续出现重大产品模型替代时，优先更新本索引的状态，而不是重写历史文档伪造当时决策。
- 当前阶段完成并通过人工验收后，应把稳定结果同步到 `product / architecture`，并将阶段 Spec 状态由 Target/Current 调整为 Implemented/Partially superseded 等真实状态。
- 新任务没有对应 Current/Target Spec 时，不得把最近一份历史 Spec 自动提升为当前合同。
- 不允许出现多个文档同时自称“最高权威”而没有冲突优先级说明。
