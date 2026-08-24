## Why

Issue #117 的人工测试确认了 9 个相互独立但需要统一收口的问题：前端信息密度和权限提示不准确，实时日志“暂停”仍会追加可见内容，PostgreSQL 半初始化可能被健康检查误判，以及 Tencent ima 的真实列表字段与现有解析契约不兼容。现在修复可以在不改变 DLR 既有保存、运行、权限和 Secret 合同的前提下，补齐用户可观察性与部署可靠性。

## What Changes

- 将 AI Assistant 附件限制与隐私说明收敛为不主动换行的单行，并保持原有限制和隐私语义。
- 将 Python 依赖、凭据绑定默认折叠；为 Monaco 代码编辑区提供最大化/恢复控制，保持 Working Copy、光标和滚动语义。
- 按平台管理员与非管理员展示不同的新增凭据引导；非管理员不出现系统设置入口，但不放宽已有绑定权限。
- 删除 Adapter Catalog 的 `catalog.overview` 常驻说明，保留标题和操作入口且不留空白。
- 让实时日志“暂停跟随”冻结前端可见快照；恢复时按顺序、去重地补齐暂停期间日志，再恢复实时跟随，并兼容日志上限/截断。
- 按现有 DLR 前端设计语言统一账号资料和用户管理页面的布局、层级、控件、状态反馈和响应式留白，不改变已有账号与权限操作。
- 在 PostgreSQL 首次初始化前校验平台日志目录存在且可写，并让 healthcheck 验证目标 `dlr` 数据库可查询，避免半初始化被标记 healthy。
- 为本地开发提供可写的 `DLR_PLATFORM_LOG_ROOT` 示例和快速开始说明，区分开发与 Linux 生产部署，明确五个子目录及 postgres 写权限要求。
- 在 ima adapter 边界将 `kb_id/kb_name` 归一化为统一知识库 ID/名称并兼容 `id/name`，继续严格拒绝两套字段均缺失的响应。

本 change 仅创建 OpenSpec 规划 artifacts；不在本批实现业务代码、运行迁移、远端交付或修改历史 Specs。

## Capabilities

### New Capabilities

- `ai-assistant-layout`: AI Assistant 底部说明单行布局合同。
- `workbench-editor-layout`: 编辑页底部区域折叠和代码编辑区最大化合同。
- `credential-binding-permission-hints`: 凭据绑定区域按平台角色展示新增凭据提示的合同。
- `adapter-catalog-layout`: Adapter Catalog 常驻说明移除后的布局合同。
- `live-log-follow-freeze`: 实时日志暂停冻结、恢复补齐和上限处理合同。
- `account-management-ui`: 账号资料与用户管理页面的统一 UI 合同。
- `postgres-init-health`: PostgreSQL 日志目录预检和真实目标库健康检查合同。
- `platform-log-local-development`: 平台日志根目录的本地开发配置与文档合同。
- `ima-knowledge-base-normalization`: Tencent ima 知识库列表双字段契约归一化合同。

### Modified Capabilities

无。当前 `openspec/specs/` 没有已登记 capability；本 change 为新增的可验证合同，归档/同步时再进入现行规格。

## Impact

- Web 组件与本地化文案：`AiAssistantPanel`、`App`、`CredentialBindingsEditor`、`AdapterCatalog`、`OutputView`、`LiveLogWorkspace`、`AccountUserPage`、`UserManagementDrawer` 及其现有测试。
- 部署与文档：`.env.example`、`docker-compose.yml`、`README.md`、`docs/deployment/platform-logs.md` 及 Compose 回归验证；不改变既有 API 路径或数据库模型。
- Backend ima adapter 与知识库测试：`list_knowledge_bases()` 的边界字段归一化及合法/旧契约/异常响应 fixture。
- 兼容性：保持现有 Adapter 保存/运行、Credential CRUD 与绑定权限、AI Candidate/Apply、SSE 连接、日志脱敏、ima search 的合同；只新增响应字段兼容分支、启动前失败门禁和文档示例调整。
- 安全：不新增凭据存储或权限；不得把 Secret、Token、Prompt、Provider 原始响应、原始 ima payload 或日志敏感内容写入浏览器、普通日志、数据库或测试输出。
- 迁移/回滚：预期无数据库 migration 和公共 API breaking change；Compose/前端/backend 改动可按文件级回滚。若实现发现需要改变 schema、权限或部署数据迁移，必须停止并重新评审本 change，不得在本批次自行扩展。
