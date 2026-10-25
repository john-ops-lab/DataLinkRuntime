# 本机代码预审与按风险合并

项目规则以根目录 [AGENTS.md](../../AGENTS.md) 为准。Open Code Review（OCR）在本机审查提交差异，Agent 核实问题、修复和验证；GitHub 保留最终 CI 与受保护合并。工具安装不改变 DLR 运行服务。

## 工具与模型

当前固定版本为 `@alibaba-group/open-code-review@1.13.3`，项目审查规则为 [rule.json](../../.opencodereview/rule.json)。安装官方便携技能后，可在下一轮 Codex 中使用 `open-code-review`；已有任务也可直接调用 CLI。

```sh
npm install -g @alibaba-group/open-code-review@1.13.3
ocr version
```

模型、API 地址和 API Key 通过本机私有配置管理，不提交到仓库。Kimi 官方 K3.1 模型 ID 为 `kimi-k3.1-pro`；第三方服务可能使用不同别名，以该服务实际提供值为准。使用 OpenAI Chat Completions 兼容协议，保留多轮工具调用所需的 `reasoning_content`；K3.1 始终思考，不套用关闭 thinking 的通用示例。

凭据建议通过 OCR 的 `api_key_cmd` 从本机私有文件或秘密管理器读取；不得把真值放到命令参数、聊天、截图、公开配置或日志中。配置的模型服务会收到所选代码和必要上下文。先做最小连接/工具调用验证，再运行真实预审；连接成功不等于审查完成。

## 提交前流程

1. 完成本次实现、必要测试和能提前完成的文档，明确验收条件和风险；大任务仍拆成可独立审查的批次。
2. 形成干净候选提交，固定 base/head 完整 SHA。背景文件包含任务目标、相关 OpenSpec、验收要求和适用项目规则，放仓库外。
3. preview 后逐项核对实际覆盖；项目规则显式包含相关测试、OpenSpec 和规则说明。排除文件必须有理由；删除文件仍要核对调用方与兼容性。必要时用 `ocr rules check <path>` 确认规则来源。
4. 执行预审，保留完整 JSON 与日志；核实实质发现后修复，运行受影响验证并复审。记录新增改动覆盖，不把旧回执冒充新候选通过。
5. 审查、验证和可提前完成的说明准备好后，再提交 PR。

以下是命令形式，变量由当前任务设置。使用不同的 preview/result 文件名；业务背景和报告路径必须位于仓库外的私有目录，不在 shell 中拼接未经转义的用户文本。

```sh
ocr review --from "$REVIEW_BASE_SHA" --to "$REVIEW_HEAD_SHA" \
  --preview --format json --output "$REVIEW_PREVIEW_FILE"
ocr review --from "$REVIEW_BASE_SHA" --to "$REVIEW_HEAD_SHA" \
  --background-file "$REVIEW_BACKGROUND_FILE" \
  --format json --audience agent --concurrency 2 \
  --effort medium --max-tokens-budget 200000 \
  --output "$REVIEW_RESULT_FILE"
```

20 万 token 是本例的总输入/输出预算，含上下文重复传入，不能解释为费用保证或每个 PR 必定够用。预算不足、超时、接口失败或文件被跳过时说明影响范围；仅补齐必要范围，不无依据扩大测试或重复全审。

## 判断预审是否完成

核对 JSON 的终态、manifest（若存在）、逐文件状态、warnings、选中/排除清单及实际模型。OCR 退出码 0 仍可能带警告或部分完成；`skipped`、`partial`、`failed` 或没有覆盖相关文件，不能当成“全部通过”。没有发现问题也不保证没有缺陷。报告中记录版本、模型、base/head、项目规则和背景摘要，并保留未解决事项。

已确认的严重缺陷修复后才交付；误报记录理由。业务决策不明确时请用户裁定。审查报告自身不修改代码，也不授权合并。委托模式复用宿主模型，应明确标注；本机 OCR 自主调用第三方模型是单独的审查过程。

## PR、合并与收尾

PR 最终 HEAD 的 `backend`、`web`、`compose-smoke`、`local-preview` 必须全部通过；有新提交就重新核对最终结果。main 变化时确认当前集成状态，不沿用已经失效的证据。需要本机功能验收时，沿用 [现有本机预览契约](local-preview.md) 与私有编排，保留卷和凭据。

低风险且满足 AGENTS.md 全部条件时，Agent 可以按本次既有授权完成 PR、等待 CI 和合并。高风险、分类不确定或用户明确要求审查的 PR，完成可审查结果后等待用户确认。当前规则不追溯撤销此前对既有 PR 的“不要自动合并”约定。任何涉及自动合并规则本身的修改都属于高风险。

合并必须通过受保护的 PR 路径，绑定核对过的完整 HEAD，不使用管理员绕过。合并后另行核对实际 main SHA 与四项 CI，再按任务授权同步主目录和开发环境。PR CI、main CI、部署、功能验证和用户验收分别记录。不要通过省略检查或混用 SHA 减少 CI。

CI/环境执行后产生的记录优先补充 PR 说明和私有验收账本；正式归档时同步合同记录。仓库文件确实需要修改时如实提交，并重新执行适用门禁，不能为避免重跑隐瞒变更。
