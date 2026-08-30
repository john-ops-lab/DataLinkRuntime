## Context

动机与范围见 [proposal.md](proposal.md)。现有 AI 附件管线已经为图片、PDF、DOCX、文本和代码提供严格 base64/schema 校验、MIME/扩展名核对、请求级内存处理、字符预算与超时；新增表格格式应沿用该管线，而不是建立第二个上传或存储通道。Issue #127 同时建立了 Managed Input 的 ArtifactStore/Binding/Lease 合同，AI 只能读取配置元数据，不能成为绕过 Execution 授权的文件下载者。

现有部署级系统语言通过公开 bootstrap 与缓存控制整个应用。登录页伴随改动需要一个独立浏览器偏好，但认证态仍没有用户级 locale 数据模型，因此必须在认证边界显式切回服务端系统语言。

## Goals / Non-Goals

**Goals:**

- 复用现有附件安全预算，为 XLS/XLSX 提供稳定、可测试的文本投影。
- 保持 AI 附件和 Managed Input 两条数据通道完全分离：前者是请求附件文本，后者只提供安全元数据和运行时 API 指引。
- 让未认证登录界面可在中英文间持久选择，同时维持认证态系统 locale 单一权威。
- 让两项伴随功能拥有独立 Gate，不污染 Issue #127 的人工验收状态。

**Non-Goals:**

- 不实现电子表格公式计算、宏、外链加载、格式还原、图表/图片提取或超大工作簿流式分析。
- 不把 Managed Input Blob 自动发送给 Provider，不复用 Execution Claim/Lease，不给 AI 新增文件读取 API。
- 不新增服务端账号 locale、locale 同步 API 或部署级语言迁移。

## Decisions

### 1. XLSX 使用受限 ZIP/XML 读取，XLS 使用单一有界解析依赖

XLSX 沿用 DOCX 已有 ZIP 单 member 解压大小、总解压大小和膨胀比检查，并在 XML 解析前对 stored/deflated member 的原始压缩流执行有界解压、CRC 与实际尺寸复核，不信任 ZIP central directory 声明值；只读取 workbook/shared strings/worksheet 单元格值，不解析关系目标、宏、样式或公式表达式。旧式 BIFF XLS 不是 ZIP/XML，采用 `xlrd>=2.0.2,<3` 的 `file_contents` 内存入口，并另外限制遍历行列与总解析时间。

备选方案是只支持 XLSX；它不能覆盖已经出现在本地需求中的历史 XLS。自行实现 BIFF 解析会引入远高于一个成熟纯 Python 依赖的安全和维护风险，因此否决。重量级数据分析库也不符合当前附件管线的小依赖边界。

### 2. 所有表格路径先验证声明，再进入已有总预算

浏览器和服务端都维护相同的扩展名/MIME 分类，但服务端始终为最终权威并检查 OOXML ZIP 或 OLE Compound File 签名。解析输出使用 tab/newline 作为稳定的行列投影，最后统一进入既有字符截断函数和 Provider 不可信附件说明。公式单元格只允许保留文件已有缓存显示值；无缓存值时为空，不求值。

此设计保证新增格式不会改变既有 PDF/DOCX/text/image 合同。浏览器预检只是体验优化，不能替代服务端拒绝。

### 3. Managed Input 使用窄数据库投影，不接触 ArtifactStore

AI assist 在已有 Session 中查询当前 `AdapterInputConfig` 与当前 revision 的 Binding，只选择 `source_type`、`ordinal`、公开原文件名和内容类型等安全标签，并按 ordinal 排序。它不获取 storage key、Artifact ID、Blob、SHA 内部授权材料或 Lease，也不锁定/改变生命周期状态。

Provider prompt 明确区分：`saved_managed_input` 是配置元数据，实际文件只在 Adapter Execution 的 `context.input_files` / `context.inputFiles` 中存在。若用户需要 AI 分析真实内容，必须走现有一次性 AI 附件合同。

备选方案是让 AI 复用 Worker 下载端点；这会扩大 Claim Token/Lease 的授权主体并破坏 Issue #127 的最小暴露合同，因此否决。

### 4. 登录偏好与系统 locale 使用两个明确命名的缓存

登录偏好使用独立 localStorage 键，只接受 `zh-CN`/`en`。未认证 LoginShell 首次无有效偏好时用产品默认 `zh-CN`，切换时同时更新局部渲染与全局 i18n，以保证表单、错误消息和 Ant Design locale 一致。localStorage 访问必须容错，不能让隐私模式或损坏值阻断登录。

登录成功后先尽力刷新公开系统 locale，并在展示认证态界面前应用它；读取失败则使用有效系统缓存或默认系统语言。强制改密属于认证态，语言选择器不应可操作，也不应写登录偏好。退出后再读取浏览器登录偏好。

备选方案是继续让登录页跟随部署 locale；它不满足用户明确保留的登录选择行为。把偏好保存到账号则需要 schema/API/权限与迁移，超出本次边界。

### 5. 同 PR、独立 change 与独立证据

`issue127-unified-input-object` 只描述 Issue #127 审计修复；本 change 描述 AI/登录伴随功能。最终可在一个 Git Candidate 中共同验证，但证据必须列出两个 OpenSpec change 的 strict 结果，且 Issue #127 的用户人工 PASS/FAIL 仍不由伴随功能自动满足。

## Risks / Trade-offs

- [恶意或畸形工作簿消耗 CPU/内存] → 复用原始附件字节上限、ZIP 膨胀边界、字符预算和超时；XLS 再限制遍历维度，并用畸形/超时 fixture 验证。
- [解析库供应链或许可证风险] → 将 `xlrd` 固定在 2.x 范围，提交锁文件，记录其仅用于 BIFF 内存解析，并运行依赖/许可证检查。
- [XLS/XLSX 显示值与 Excel 视觉结果不完全一致] → 明确只提供单元格文本/缓存值，不承诺格式、合并单元格、图表或公式重算。
- [文件名进入 prompt 形成提示注入] → 继续把所有附件与文件标签标记为不可信数据，做既有文件名清洗，并验证其不能覆盖 system contract。
- [登录 locale 在异步 bootstrap 时闪烁] → 首次渲染直接由同步读取的有效登录偏好决定；认证态在 principal 页面显示前应用系统 locale。
- [两个 locale 缓存相互污染] → helper 与测试分别断言登录偏好键和系统 locale 键；认证切换只写系统缓存，登录选择只写登录偏好。

## Migration Plan

1. 先加入后端解析/元数据投影与单元测试，再接入 Web 类型、文案和登录行为。
2. 更新依赖锁与中英文产品文档，明确附件格式及登录/系统语言边界。
3. 在同一 Candidate 上运行 AI focused/backend Gate、登录 focused/Web Gate、两个 OpenSpec strict 和最终回归。
4. 回滚时移除 XLS/XLSX 分类和 AI 元数据提示，再恢复登录页跟随系统 locale；无需数据库 downgrade。遗留的登录偏好 localStorage 键无副作用，可在后续版本读取或忽略。
