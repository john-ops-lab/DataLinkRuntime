# Wave B 本地确定性验证记录（2026-10-01）

- 当前 user 消息以 `DLR_REQUEST_CONTEXT_V1` 标识，JSON 内的 `AUTHORITATIVE_STATE_DATA` 承载当前 Working Copy、base version、授权 Secret 名称与保存输入投影；`UNTRUSTED_REFERENCE_MATERIAL` 只在本轮有 Snippet 或解析附件时出现。原生图片保留同一 user 消息中的独立 image parts，第一部分为结构化文本。
- System 只包含静态规则、当前 locale、代码生成的 `AiModelOutput` Schema 与对应语言 Runtime Contract，以及按实际工具/材料加载的说明。测试断言动态代码、Secret 名称、Snippet 与附件正文均不在 System 中；历史角色顺序和 assistant strict JSON 包装保留。
- 工具关闭与知识检索关闭的普通请求不带知识工具规则；显式开启时保留原 Knowledge 状态机及不可用路径。现有 Tool/Knowledge/Attachment/Managed Input、严格输出与 Candidate Apply 边界的 Backend 回归已覆盖。Fake Provider 测试只证明组装及服务端约束，不作为模型质量证据。
- 隔离 PostgreSQL 上运行相关 Backend AI 测试：394 passed。Ruff check、Ruff format check、Mypy 均通过。wheel、sdist 中均检查到三个静态 Markdown 资源。
- OpenSpec 2.3 仍待真实 Provider Smoke Test 与其余最终验收；本记录不宣称普通问答、Java/Go 字面要求或首轮/有界重试的模型行为已通过。
