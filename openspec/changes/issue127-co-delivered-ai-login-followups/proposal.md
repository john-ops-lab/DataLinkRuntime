## Why

Issue #127 的本地 Candidate 同时包含两项已经完成、但不属于统一输入对象核心合同的产品变化：AI 助手读取表格附件及已保存 Managed Input 元数据，以及登录页独立语言偏好。用户已明确要求两项改动保留在同一 PR；本变更为它们建立独立、可审计的规格与验收边界，避免把伴随功能伪装成 Issue #127 的验收项。

## What Changes

- AI 助手附件增加旧式 XLS 与 XLSX 的有界、纯内存文本提取；不执行公式、宏或外部关系，不持久化附件内容。
- AI 助手在生成 Adapter 建议时可读取当前 Adapter 已保存的 Managed Input 安全元数据与稳定 Context 文件 API 提示，但不得读取 Blob、泄露内部标识、路径、Token 或文件内容。
- 未认证登录页增加仅保存在当前浏览器的 `zh-CN` / `en` 语言偏好；首次无偏好时使用 `zh-CN`，不由部署级系统语言静默决定。
- 认证完成后的控制台与强制改密页继续遵守服务端系统语言，不引入服务端用户级语言档案。
- 更新双语文案、产品文档和测试，使上述边界在同一 PR 中可独立验证。

非目标：不把 Managed Input 文件自动附加给模型，不提供 AI 读取 ArtifactStore 的通道，不解析或执行电子表格公式/宏，不增加每用户服务端 locale，不改变 Issue #127 的 managed-files 生命周期与人工验收事实。

## Capabilities

### New Capabilities

- `ai-spreadsheet-and-managed-input-context`: AI 表格附件解析、Managed Input 安全元数据投影、Provider prompt 边界与错误合同。
- `login-locale-preference`: 未认证登录语言的浏览器级偏好，以及认证后恢复服务端系统语言的边界。

### Modified Capabilities

无。当前仓库没有已同步到 `openspec/specs/` 的现行 capability；本变更不改写历史 `docs/specs/`。

## Impact

- Backend：`dlr.control.ai.attachments`、AI assist context 组装、相关 schema/测试；新增受版本约束的 `xlrd` 运行时依赖及锁文件变化。
- Web：附件类型识别、AI 助手提示、登录页/账户登录页/强制改密页、locale helper、i18n、样式与测试；继续保持 React 19、Ant Design 5.29.3、ProComponents 2.8.10。
- 安全与数据：附件仍仅在请求内存中处理，不写临时文件、数据库或日志；Managed Input 只投影公开显示元数据；登录偏好只写浏览器 localStorage，不进入账号资料。
- 兼容与回滚：现有 PDF/DOCX/text/code/image 附件行为保持；XLS/XLSX 是向后兼容新增。回滚可移除新附件类型与浏览器偏好读取，不涉及数据库迁移；旧 localStorage 键可安全忽略。
- 交付：与 `issue127-unified-input-object` 在同一 PR 合并，但拥有独立规格、测试和证据；Issue #127 的人工验收仍单独判定。
