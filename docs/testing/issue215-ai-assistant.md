# Issue #215：AI 助手布局与代码注释验证

## 交付范围

依据[已确认的 v2 设计](../evidence/issue215/design-v2.png)：输入区只保留附件和发送；顶部更多菜单仅提供使用说明；按后续确认移除了查看发送内容入口及弹窗。输入文字与对话正文共用字号和行高。详情以独立对话框打开，不挤压输入与发送操作。侧栏约 400–440px；最大化对话和输入列不超过 904px。当前模型及临时对话标识持续显示，代码建议提供只读预览，应用仍通过原有 Diff 流程。

统一 Prompt 要求候选代码充分加注释，说明用途、输入输出、字段映射、关键步骤、判断和异常处理，并解释不直观实现的原因。注释语言以服务端系统配置为准，不由用户消息、编程语言、附件或历史内容决定。已有有效注释应保留原意、按当前系统语言翻译并随逻辑更新；返回前核对说明与实际运算符和库行为，代码仍须完整、语法有效且符合运行契约。

## 可复现检查

在仓库根目录运行：

```sh
npm --prefix web ci
npm --prefix web run lint
npm --prefix web run build
npm --prefix web run test -- src/assistant-wave-b.test.tsx src/assistant-wave-b3.test.tsx src/assistant-wave-c2-knowledge.test.tsx --maxWorkers=1 --minWorkers=1
npm --prefix web run test -- src/App.test.tsx -t 'renders the B2 attachment capability bounds|shows the assistant privacy guidance and the quiet assistant header' --maxWorkers=1 --minWorkers=1
DLR_B6_OUTPUT_DIR=../../test-results/issue215-legacy npm --prefix web run test:browser -- issue215-ai-assistant.spec.ts issue117-b6-ai.spec.ts
uv run --frozen --project backend ruff check backend/src/dlr/control/ai/prompt_builder.py backend/tests/test_ai_prompt_builder.py
uv run --frozen --project backend ruff format --check backend/src/dlr/control/ai/prompt_builder.py backend/tests/test_ai_prompt_builder.py
```

Prompt builder 测试需按项目测试环境配置必要的 RabbitMQ URL 环境变量（该文件不连接 RabbitMQ），然后执行：

```sh
uv run --frozen --project backend pytest backend/tests/test_ai_prompt_builder.py backend/tests/test_m5_6_wave4_e_scan.py -q
```

## 初版自动验证记录

- 相关前端测试：85 项通过，覆盖菜单、即时草稿／附件预览、焦点恢复、Escape 层级、只读代码预览，以及原有附件、重试、重新生成、临时状态与 Diff 边界。
- Prompt builder 119 项及国际化扫描 4 项通过，覆盖 auto／code_change／consultation 三种意图、五种编程语言、中文／英文、工具／知识／附件组合与相反语言的用户输入。
- 新增浏览器矩阵：中英文 × 1280／1440／1920，共 6 项通过；包含长输入、长文件名、菜单与详情、最大化／恢复、候选预览与 Diff。
- 既有附件布局矩阵改为通过更多菜单检查完整帮助，中英文 1100／1180／1280／1440／1680／1920 共 12 项通过；仍检查选择、拖入、拒绝、删除和发送。
- lint、TypeScript、生产构建、Ruff、单文件 mypy 与锁定版本 Ant Design lint 均通过。

浏览器回归使用合成 API／Provider 夹具，证明渲染和交互，不证明真实模型质量。真实 Provider、部署 SHA、Chrome 及本地验收结果在 PR 交付记录中单独报告。首次测试夹具缺失、负载超时、国际化扫描对内联箭头表达式的误识别及其修正与重跑记录保留在私有验证材料中；不将其改写为首轮通过。

## 本地验收检查路径

1. 打开任意可编辑适配器，展开助手，核对只有附件和发送两个输入工具栏操作。
2. 输入草稿并加附件，核对更多菜单仅有使用说明；关闭帮助后核对焦点、草稿、附件及发送位置，并比较输入框与对话正文的字号、字体和行高。
3. 打开使用说明，核对真实附件限制、模型能力、临时聊天和人工应用说明；Escape 先关闭详情，不应同时退出最大化。
4. 在两种系统语言分别用相反语言提出代码修改请求，检查候选代码中的注释语言、信息量、准确性与目标语言语法。
5. 检查重新生成、附件驱动生成及系统语言变更后的新请求；预览／Diff 不改代码，只有人工 Apply 才改浏览器草稿，保存和运行继续单独执行。

## 真实 Provider 与运行验证记录

使用验收环境已配置的 MiniMax-M3，以合成数据调用真实 Provider，未更改凭据或模型配置。模型质量和实际业务输出分别判断：HTTP 成功、生成候选和执行成功均不单独视为业务通过。

- 初轮 16 个建议涵盖五种语言、中英文、修复、重新生成请求和附件。发现中文 Go 修复保留旧英文注释，以及 Python 对无穷值的说明和实现不一致。32 个运行检查中 31 个符合独立预期，1 个最终取消；失败、重试及取消记录保留。
- 针对性第二轮 8 个建议中，注释语言检查通过；原始 16 个运行检查中 15 个通过，中文 TypeScript 附件建议误将空白字符串金额视为零。明确修复该业务问题后，正常数据与非数组输入 2 项通过；以修复后候选替换原候选的最终集合为 16／16，不能据此称原始集合全部通过。
- 真实页面继续发现：完整旧代码的小改动仍可能复制相反语言注释。提示词增加服务端语言事实，将语言要求放在系统段末尾，并明确复制的注释也必须本地化，注释文字的本地化优先于最小改动；编译指令、标识符和数据字面量保持原义。补强后在真实 Provider 上验证完整中文代码的小改动返回英文注释，以及完整英文代码的小改动返回中文注释；英文页面重新生成也返回英文注释，未要求用户显式指定注释语言。

独立运行预期：9 行混合数据包含有效金额、空标识、负数、NaN、布尔值、空白金额、非字符串标识和 Infinity，预期 acceptedCount=2、invalidCount=7、total=6；rows 为 null 时三项均为零。检查保存的输入配置修订、不可变代码版本、执行输入快照与实际输出。

### 真实 Chrome 集成结果

中英文 × 1280／1440／1920 六组检查通过：侧栏宽度 400／432／440px，最大化阅读列上限 904px；长草稿、长文件名、更多菜单、详情焦点、Escape、折叠和最大化恢复均未丢失输入，发送工具栏不被挤出视口。

真实附件回合及英文重新生成已验证原回合两份附件与当前未发送草稿／新附件相互独立。随后服务端改为中文而页面保持英文时，一次附件重新生成触发 `ai_tool_duplicate` 保护，未生成可用最终候选；此项保留为未通过，不能记作中文附件重新生成成功。独立的当前代码请求随后返回中文注释，证明输出跟随当前服务端配置；完整英文旧代码转中文的真实 API 请求也通过。

当前代码建议经只读预览、原有 Diff、人工 Apply、显式保存、运行，真实页面与独立 API 均观察到 acceptedCount=2、invalidCount=7、total=6、source=ui-215。保存前不可运行；预览和打开 Diff 不使保存按钮变为可用。Provider 的重复工具调用与生成业务错误仍需按现有审查流程处理，本次不宣称任意模型输出或全部附件回合均成功。

实现提交 `29178e4` 的完整 CI：Web 854 项，后端 2868 项通过、17 项跳过，另两项门禁通过。最终 PR HEAD 的 CI、同一私有验收环境部署 SHA 及部署后冒烟结果以 PR 交付记录为准。私有入口、配置、凭据与部署参数不进入仓库。

## 后续简化

按用户后续确认删除「查看发送内容」入口及其弹窗，更多菜单仅保留使用说明；输入框与对话正文共用字号和行高规则。直接相关的菜单／帮助／草稿回归 14 项和中英文 1440px 字号浏览器检查 2 项通过；Web lint、类型检查、构建、锁定版 Ant Design lint 和 OpenSpec 严格校验通过。本次不修改 Prompt，不追加真实模型或 Worker 业务矩阵。最终提交的 CI 与现有本地部署结果见 PR 交付记录。
