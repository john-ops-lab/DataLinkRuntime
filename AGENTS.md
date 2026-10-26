<!-- antd-cli setup start -->
## Ant Design CLI Skill

Use the shared Ant Design skill at `.agents/skills/antd/SKILL.md` before working on Ant Design code in this repository.

The skill teaches agents when and how to call `@ant-design/cli` commands such as `antd info`, `antd doc`, `antd demo`, `antd token`, `antd semantic`, and `antd changelog`.

## DLR Ant Design version boundary

- This repository's Web baseline is `react` 19, `antd` **5.29.3**, and `@ant-design/pro-components` **2.8.10**. Keep these exact versions in the Web manifest unless a later Wave explicitly changes the contract.
- Use the project-local skill and query the exact snapshot before writing or changing Ant Design code. The reproducible CLI form is `npx --yes @ant-design/cli@6.6.1 --version 5.29.3 <command> --format json`.
- For API, Demo, Design Token, Semantic DOM, and changelog questions, run `info`, `demo`, `token`, `semantic`, and `changelog` respectively. Do not infer an API from memory or from the unversioned latest docs.
- Ant Design 6, Ant Design X, Umi, Tailwind, and a second general-purpose UI framework are outside the current M5.10 Wave A contract.

<!-- antd-cli setup end -->

## 按改动影响范围验证

- 根据实际改动及受影响的用户路径确定最小必要验证范围，优先运行直接相关的测试和代表性端到端检查；不因涉及 AI 功能就默认展开全语言、全场景或多轮真实模型／Worker 验证矩阵。
- 当前范围的必要检查通过后，继续完成交付。只有本次改动引入的新风险、失败或未解决疑点，才针对具体原因补测，不无依据扩大或重复测试。
- 额外发现的问题单独记录现象、证据、影响及后续事项，不自动追加修复或扩大测试范围拖住当前交付。若直接阻断既定验收，仅做定位该阻塞所需的最小排查，并明确报告；扩大任务范围须有用户授权。
- 项目规定的 CI、精确 HEAD 和部署门禁照常执行；控制验证范围不代表跳过必要门禁。

## 本机 PR 验收交付

- 本机验收入口及部署参数仅从私有配置读取，禁止写入公开 PR 或仓库。控制器源码与操作契约见 [docs/zh-CN/local-preview.md](docs/zh-CN/local-preview.md) 和 `tools/local-preview/`。
- 用户要求提交 PR 并提供本地验收时，在 PR 建好后执行 `python3 "$DLR_PREVIEW_HOME/preview.py" select <PR>`，由控制器等待当前 HEAD 的 CI 通过再更新；查询 `status`，报告已部署 SHA 与真实验证结果。不要另建普通后继 PR 的重复环境；合并遵循下方风险规则，用户明确“不要自动合并”时必须保留 PR。
- 保留固定 Compose project、持久卷、Token 与 Master Key。历史分叉、迁移不兼容或 attention 时先诊断，不绕过检查，不自动回退数据库。


## 本机预审与 PR 交付

- 默认顺序：本机实现与可提前完成的文档 → Open Code Review（OCR）预审 → 核实并修复确定问题 → 针对性验证与复审 → 提交 PR → 当前最终 HEAD 的四项 CI → 按风险合并 → 核对合并后 main CI。操作指南见 [docs/zh-CN/local-code-review.md](docs/zh-CN/local-code-review.md)。
- 本机使用固定 OCR 版本 `1.13.3` 和项目 `.opencodereview/rule.json`；模型、API 地址和凭据只放用户私有配置。需要升级工具或更换模型时按当前任务授权处理，不静默替换。业务背景包含本次目标、相关 OpenSpec、验收要求和适用的 AGENTS.md。
- 预审先 preview，检查实际选中、排除及删除文件；相关测试、OpenSpec 和规则文档不能因默认过滤而漏审。生成物和敏感材料排除；删除文件的调用方和兼容性另外核对。审查报告、日志与运行证据放仓库外的私有目录。
- 提交候选应干净、可复现；记录审查的完整 base/head SHA、OCR 版本、模型、规则和背景摘要。修复后先核实代码和触发条件，再运行受影响的必要验证并复审；新的变更必须进入审查范围，不复用不匹配的旧回执。
- 命令退出 0 或没有评论不能单独算通过：检查终态、逐文件完成/排除/失败、warnings 与预算/超时。相关文件未覆盖、partial、failed、结果未知时，报告未完成并处理具体原因；不得标成全审通过。
- OCR 报告是待核实证据。优先修复确定的功能、权限、数据、恢复和明显性能问题；不因风格建议扩展重构或测试。业务规则无法裁决时保留问题给用户决定。宿主委托审查须注明实际由宿主模型执行，不能冒称独立第三方模型审查。
- 保留 `backend`、`web`、`compose-smoke`、`local-preview` 四项 CI；本地验证不替代它们。合并前核对最终 HEAD、当前 main 的集成结果与未解决审查意见；main 前进导致原集成证据失效时重新验证。主目录及部署同步都绑定明确 SHA。

## 收尾与重复 CI 控制

- 能在原 PR 合并前完成、且在已授权范围内的文档、正式规格同步、升级说明和归档改动，随原 PR 一并提交与审查；不要常态化留到合并后再补。不提前填写尚未发生的验证结果或完成状态。
- 合并后才产生的 CI、部署及功能验证结果、实际 SHA 等动态证据，补到原 PR 说明、相应 Release（如有）和私有验收记录；不为记录这些结果单独增加仓库提交或“收尾 PR”。关闭 Issue、同步本地目录、打标签和发布 Release 等操作，在没有仓库文件变更时不另开 PR。
- 只有合并后确实有新的仓库内容需要修改时，才另开 PR，并说明新增内容及必要性；如实提交实际变更，不为减少 CI 隐瞒或拖延必要修改，也不把大变更堆成难审的大 PR。
- 通过提前整合改动减少重复 CI，保留本机 OCR、按影响范围验证、最终 HEAD 四项 CI、合并前当前 main 集成检查、合并后 main CI，以及已要求的部署和验收门禁。新增提交或集成基线变化后重新核对适用门禁，不混用旧 SHA 的回执，不绕过 main 保护；原有按风险合并及人工确认规则继续适用。

## 按风险合并

- 用户当次明确要求“先不提 PR”“不要自动合并”“给我审查”时优先；既有 PR 的人工确认约定不因本节新增而撤销。只分析、只审查时不修改、不提交、不合并。
- 满足全部条件的低风险改动，允许 Agent 无需再次询问而创建 PR、等待检查并合并：范围小、行为和验收明确、可回退；本机预审与必要验证完成；没有未解决的实质问题；当前最终 HEAD 四项 CI 成功；当前 main 的集成检查有效；已要求的本机功能验证完成；没有人工确认约定。
- 低风险候选包括不改变外部合同/操作含义的文案与说明、补充而不削弱断言的测试，以及不涉及下述高风险面的局部修复。不能仅凭文件后缀或“只改几行”判定风险；拿不准按高风险处理。
- 以下高风险变更先完成实现、预审、针对性验证和必要真实场景验证，再提交 PR 给用户确认：权限/认证/凭据与安全边界；数据库迁移与数据处置；消息接收、持久责任、确认、去重、重试、取消与恢复语义；外部 API/数据合同；核心并发、资源隔离、大规模重构；依赖基线升级；AGENTS.md、审查规则、CI、分支保护、部署或自动合并规则本身。
- 自动合并仍走受保护 PR 路径，绑定已核对的完整 HEAD SHA；禁止 admin/force/bypass 或直接 push main 绕过门禁。仓库保护或权限不足时报告具体阻塞，不擅自降低保护。合并后独立核对实际 main SHA 与四项 CI；失败时报告并做最小定位，不把 PR CI 当成 main CI。
