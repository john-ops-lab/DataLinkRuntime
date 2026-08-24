# Issue #117 Batch 2 ima 浏览器验证

## 范围与环境

- 路径：`系统设置 → 知识库 → 测试连接`，随后重复触发测试连接以刷新列表。
- 前端：`http://127.0.0.1:4173/settings/knowledge-sources`。
- Provider：本地 fake/fixture provider（`127.0.0.1:9000`），返回新字段 `kb_id` / `kb_name`。
- 凭据：仅使用匿名 placeholder fixture；未使用真实 Tencent ima 凭据，也未保存或回显 Secret。

## 结果

- UI 显示 `连接成功，知识库列表已刷新`。
- 知识库表显示 `浏览器 Fixture 知识库`，状态为 `可访问`。
- 安全 API 结果见 `ima-api-fixture.json`，只返回 normalized `id` / `name` / `status` metadata。
- AO Browser 页面快照和可见文本见：
  - `ima-settings-knowledge.snapshot.json`
  - `ima-settings-knowledge.page.json`
- AO Browser 有界脱敏请求 metadata 见 `ima-settings-knowledge.network.json`：第二次刷新为 `POST /api/knowledge-sources/ima/test`，HTTP `200`；未保存 headers 中的鉴权值、请求体或响应体。
- Console 与错误证据见：
  - `ima-settings-knowledge.console.json`：仅 Vite/React/i18next informational messages。
  - `ima-settings-knowledge.errors.json`：`messages: []`。

## Overflow

使用同一 fake/fixture 环境补充 Chromium 读数，页面路径和交互与 AO Browser 相同。`ima-settings-knowledge.overflow.json` 在 `1280/1440/1680/1920 × 900` 下均记录：

- `horizontalOverflow: false`
- `verticalOverflow: false`
- 知识库结果可见，`pageErrors: []`

1440px 页面截图：`ima-settings-knowledge.playwright.png`。

真实外部凭据不可用且未尝试；因此本证据证明的是 fake/fixture provider 下的 UI、请求和安全 metadata 链路，不是 Tencent ima 真实环境验收。
