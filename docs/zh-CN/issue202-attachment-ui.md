# AI 附件准备与前端生命周期（#202）

AI 面板沿用 assistant-ui、当前页面临时对话、Candidate → Diff → 显式 Apply，以及人工 Save/Test/Run 的已有合同。图片、PDF、Office 和文本的文件选择仍由服务端类型与大小能力限定，不按 Provider 品牌猜测视觉支持，也不自动切换所选模型。

## 准备与覆盖

面板从 `GET /api/adapters/{id}/ai/attachment-capabilities` 获取当前 Adapter 编辑范围的能力。`selected` 是当前模型、实际 endpoint/protocol 的有效能力；`providers` 仅为目录/声明。`prepared_attachments_enabled=true` 时，选择文件后向 `POST /api/adapters/{id}/ai/attachments` 提交 `{scope_id, attachment}`，其中 attachment 继续沿用 filename/content_type/data_base64。准备、部分成功、失败与过期分别显示；部分成功不会显示为已完整解析。

准备响应只包含来源目录与安全元数据：attachment_ref、sha256、字符串 representation_revision、filename/content_type/size_bytes、ready/partial、原始 1-based 页号、工作表、coverage、稳定 warnings 和真实 expires_at。页面显示空白页原号、失败或未准备页、sheet/range/打印区域及公式缓存缺失说明；未知 warning 使用通用覆盖提示，不显示原始内部 code。目录显示上限 64 页与 32 张表，折叠区可滚动；其余材料仍由服务端有界工具按需读取。目录与“已准备”不代表模型已阅读全文。

转换不可用或旧后端缺少新字段时，保留旧 inline attachments 请求；PDF/Office 明确提示仅有界文本降级。未知视觉能力或仅管理员声明的 custom 能力有对应说明，不把文件类型自动视为视觉支持。旧 Control 若缺少 Adapter 范围 capability 路由，客户端仅对普通 missing-route 404 回退到旧全局路由；权限和资源 domain 错误不会回退。

## 冻结与释放

每个挂载的账号/Adapter 范围使用内存 UUID scope。发送时冻结 attachment_scope_id 与 refs 的 attachment_ref/sha256/representation_revision，目录副本随原轮次保存；不把文件正文或 base64 展示在聊天，也不持久化聊天。Assist 只发送 prepared refs；旧 inline 兼容路径保持原样。

资源 registry 只管理 composer、可见冻结轮次和进行中请求的引用，不另建 history/Candidate 状态。删除 composer 中的文件仅在未被冻结或活动请求引用时释放单 ref。Retry/Regenerate 先用 `GET /api/adapters/{id}/ai/attachments/{ref}?scope_id=...` 校验原引用与表示；不读取新 composer 文件，不重新准备一个替代原件。过期或表示变化明确提示重新上传并发送新轮次。

移除正在准备的行使用 AbortController；迟到准备若仍返回 ref，会尝试释放该 ref，不能进入新范围。账号/Adapter 切换与组件卸载取消请求并发起 `DELETE /api/adapters/{id}/ai/attachments/scopes/{scope_id}`；同页收起/再打开保留范围。React StrictMode 的 effect replay 不会误关闭仍挂载的范围。

真实 pagehide 发起带 keepalive 的范围释放；通知可丢失，所以浏览器发起请求与服务端清理完成必须分别核验。服务器 TTL、所有权、活动 pin/refcount 和取消/进程资源回收仍是服务端合同。前端 AbortSignal 或收到 HTTP 200 不证明转换子进程已经终止。

## 验证边界

Node24 单元/组件/API 回归覆盖冻结、过期、拒绝更新表示、取消与迟到结果、StrictMode、权限错误不回退、未知 warnings 和中英文目录。隔离 Playwright 使用合成 API/文件，检查 refs-only、冻结 Retry、单 ref 释放、服务端 expired 响应不进入模型、真实 pagehide 的 keepalive 释放尝试与刷新后的空会话；不把缺少 unload 回执写成资源清理成功。

真实图片/PDF/XLSX/XLS/DOCX、真实转换和 Vision 模型、页/表格工具与预算、账号/Adapter 权限、TTL 及实际资产/进程回收由主 Agent 在集成 SHA 上验收。隔离界面回归不替代这些业务结果。

Node 24 验证：session/API/Panel 专项 56 项通过；完整 Web 套件 55 文件、713 项通过（117.23 秒）；typecheck、lint、生产构建通过；AntD CLI 6.6.1 / 5.29.3 对改动面板检查 0 项问题。双语隔离 Playwright 4 项通过（13.5 秒）。首轮全套发现旧 capability fixture 仅匹配全局路由，修正为本次 Adapter 范围合同后最终全套通过。

## BFCache 返回（#211）

浏览器离开页面时会中止等待、隔离迟到结果并释放旧附件 scope。若 Back/Forward 从 BFCache 恢复同一 React 树（`pageshow.persisted=true`），面板创建新 UUID scope，清空旧临时会话、Candidate 和 Composer，刷新能力元数据。已释放的 scope 不会重新打开，旧轮次也不会暗中重准备。普通首次 `pageshow` 不清空当前会话。

三项双语/迟到响应生命周期回归使用真实组件上的合成页面事件，验证新文件准备、refs 发送及旧响应隔离；这不代表真实 Chrome 已将页面纳入 BFCache，浏览器资格与真实 Back/Forward 恢复仍需独立验收。

#211 验证：Node 24 附件/Panel 相关 59 项通过（含 3 项新增 lifecycle）；最终 lifecycle 专项 3 项通过；typecheck、改动文件 ESLint、Pinned AntD 检查通过。未重复无关全 Web 套件，也未将合成事件标记为真实 BFCache 验收。
