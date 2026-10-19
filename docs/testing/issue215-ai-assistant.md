# Issue #215：AI 助手布局与代码注释验证

## 交付范围

依据[已确认的 v2 设计](../evidence/issue215/design-v2.png)：输入区只保留附件和发送；顶部更多菜单提供查看发送内容和使用说明。详情以独立对话框打开，不挤压输入与发送操作。侧栏约 400–440px；最大化对话和输入列不超过 904px。当前模型及临时对话标识持续显示，代码建议提供只读预览，应用仍通过原有 Diff 流程。

统一 Prompt 要求候选代码充分加注释，说明用途、输入输出、字段映射、关键步骤、判断和异常处理，并解释不直观实现的原因。注释语言以服务端系统配置为准，不由用户消息、编程语言、附件或历史内容决定。已有有效注释应保留原意、按当前系统语言翻译并随逻辑更新；返回前核对说明与实际运算符和库行为，代码仍须完整、语法有效且符合运行契约。

## 可复现检查

在仓库根目录运行：

```sh
npm --prefix web ci
npm --prefix web run lint
npm --prefix web run build
npm --prefix web run test -- src/assistant-wave-b.test.tsx src/assistant-wave-b3.test.tsx src/assistant-wave-c2-knowledge.test.tsx --maxWorkers=1 --minWorkers=1
npm --prefix web run test -- src/App.test.tsx -t 'renders the B2 attachment capability bounds|shows the M5.5.13 credential guidance' --maxWorkers=1 --minWorkers=1
DLR_B6_OUTPUT_DIR=../../test-results/issue215-legacy npm --prefix web run test:browser -- issue215-ai-assistant.spec.ts issue117-b6-ai.spec.ts
uv run --frozen --project backend ruff check backend/src/dlr/control/ai/prompt_builder.py backend/tests/test_ai_prompt_builder.py
uv run --frozen --project backend ruff format --check backend/src/dlr/control/ai/prompt_builder.py backend/tests/test_ai_prompt_builder.py
```

Prompt builder 测试需按项目测试环境配置必要的 RabbitMQ URL 环境变量（该文件不连接 RabbitMQ），然后执行：

```sh
uv run --frozen --project backend pytest backend/tests/test_ai_prompt_builder.py backend/tests/test_m5_6_wave4_e_scan.py -q
```

## 自动验证

- 相关前端测试：85 项通过，覆盖菜单、即时草稿／附件预览、焦点恢复、Escape 层级、只读代码预览，以及原有附件、重试、重新生成、临时状态与 Diff 边界。
- Prompt builder 59 项及国际化扫描 4 项通过，覆盖五种编程语言、中文／英文、工具／知识／附件组合与相反语言的用户输入。
- 新增浏览器矩阵：中英文 × 1280／1440／1920，共 6 项通过；包含长输入、长文件名、菜单与详情、最大化／恢复、候选预览与 Diff。
- 既有附件布局矩阵改为通过更多菜单检查完整帮助，中英文 1100／1180／1280／1440／1680／1920 共 12 项通过；仍检查选择、拖入、拒绝、删除和发送。
- lint、TypeScript、生产构建、Ruff、单文件 mypy 与锁定版本 Ant Design lint 均通过。

浏览器回归使用合成 API／Provider 夹具，证明渲染和交互，不证明真实模型质量。真实 Provider、部署 SHA、Chrome 及本地验收结果在 PR 交付记录中单独报告。首次测试夹具缺失、负载超时、国际化扫描对内联箭头表达式的误识别及其修正与重跑记录保留在私有验证材料中；不将其改写为首轮通过。

## 本地验收检查路径

1. 打开任意可编辑适配器，展开助手，核对只有附件和发送两个输入工具栏操作。
2. 输入草稿并加附件，从更多打开查看发送内容；关闭后核对焦点、草稿、附件及发送位置。
3. 打开使用说明，核对真实附件限制、模型能力、临时聊天和人工应用说明；Escape 先关闭详情，不应同时退出最大化。
4. 在两种系统语言分别用相反语言提出代码修改请求，检查候选代码中的注释语言、信息量、准确性与目标语言语法。
5. 检查重新生成、附件驱动生成及系统语言变更后的新请求；预览／Diff 不改代码，只有人工 Apply 才改浏览器草稿，保存和运行继续单独执行。
