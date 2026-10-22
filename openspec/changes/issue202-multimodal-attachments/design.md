# Design

## Context

初始设计基线的 JSON Assist 携带 inline base64，附件解析是文本降级；其余初始问题及动机见 proposal.md。v0.6.0 已提供 prepared 表示、真实页图、原件范围读取、三协议 SDK、预算及冻结引用。下列既有设计保留其历史阶段；本次收尾以 `5d71062342827fa2cd8b1723454c32066bd08c76` 的实际代码为准，不恢复旧 Assist 运行器。

## Goals / Non-Goals

**Goals:** 保持一次性调试，提供原件身份、真实视觉和精确表格交叉核对；大文件以目录及按页/范围读取进入既有有界只读工具链；对取消、超时和内存提供实际进程与存储证据。

**Non-Goals:** 不增加业务输入资产、数据库附件表、持久聊天、自动模型切换、自动 Save/Run、通用后台任务平台或 Docker socket 权限。

## Decisions

### 2026-10-22 文本提取补充

首轮真实 HTTP 在两家模型均复现 1/5 提取前缀／标签问题；两版单纯提示候选仍有失败，保留所有尝试。为普通完整 inline 文本附加至多 8 个字面 `text_fields` 提示，每个标签最多 80 字符、值最多 256 字符，并保留原始行号及值的字符跨度；仅拆分首个中英文冒号，不解码、不执行、不推断用户意图。原文保持，截断文本不生成提示。提示与原文都位于 `UNTRUSTED_REFERENCE_MATERIAL`，不获得工具、Secret 或代码权限，并进入原有 D6 预算。精确字段提取与整行复述分开定义，咨询回答遵循用户请求的格式。候选在隔离路由／SDK 进程中对每家 3 次原 A16、1 次完整行、1 次普通业务字段均通过；这不冒充部署后的 HTTP 验收，最终须另行复验。

### 1. 复用成熟文档组件，按同样本选择组合

固定 Docling Serve CPU v1.37.0 和 Gotenberg v8.37.0 的 OCI digest，Docling 依赖及预装模型与该镜像一致；中文扫描采用已实测的 RapidOCR Torch `lang=[ch]` preset，运行时禁止下载模型。使用 Gotenberg 所含固定 LibreOffice 与 Noto CJK 字体，精确 XLSX 读取增加固定 openpyxl，XLS 复用 xlrd。部署清单记录实际版本、镜像、字体、权重与许可。

同一套中文表格、彩色图表、空白页、扫描件、带合并/格式/公式的 XLSX 与真实 BIFF XLS 已揭示：Docling 原生工作簿表示遗漏图表/格式，旧 XLS 无可用 LibreOffice；Office 渲染成 PDF 后图表可读，outline 可精确给出源 sheet 的 PDF 起始页。数字 PDF 的结构表格即使转换 status=success 也可能列错位，因此结构只能作辅助，并保留真实页图和部分覆盖说明。详细证据保存在私有验收材料，仓库文档仅记录可复现样本与结论。

### 2. 小型隔离转换服务，逐次监督实际进程

正式 Compose 提供仅内部可见的 CPU 文档转换服务，控制节点只调用受限 API。每次重解析由独立子进程/进程组执行，设墙钟、内存、PID、临时目录和输出字节硬限制，取消或期限到达后 kill/wait 并检查目录释放。Docling Serve Local 线程池客户端超时后仍持续转换的实测反证禁止其作为硬取消实现。Office 确定采用固定 Gotenberg 镜像内 LibreOffice 的薄进程监督入口：同样本直接转换与 HTTP 入口的逐页文本、outline 和首页像素一致；HTTP 断开不能保证转换立即终止。正式服务启用 init 收割孤儿，取消必须有界确认整个进程组已消失，无法确认则返回 cancel_failed，不能提前确认 terminated。

目录/页数先有界取得，不将全部页图塞进首轮。PDF 保留原始 1-based 页号，页图和 OCR 按单页请求生成；Office 保存派生 PDF 及真实 outline sheet→页范围，可按页读取。每个工作簿记录 hidden/print-area/merged-range/chart 概况，精确原件读取保留 raw/type/number_format/formula/cached value，派生页显示值允许 LibreOffice 重算但明确来源。页图标记可读像素、MIME、尺寸、哈希，拒绝 placeholder。无结构信息时标 unknown/partial，不推断内容完整。

### 3. 独立临时附件库与最小 API

Control 使用独立权限受限临时目录及内存索引，不复用 Managed Input Store。所有权由认证 Principal、Adapter 和 browser `scope_id` 共同构成；opaque reference 不能单独授权。每次 API 再检查 edit 权限，Assistant 工具只取得该次 frozen reference 白名单。

新增 prepare/get/release 与 scope-release 路由，文件仍沿用既有 AiAttachment 字节/MIME/签名/ZIP 门禁。准备返回 `attachment_ref/sha256/representation_revision`、原件大小、目录、状态和覆盖，不返回路径或二进制；Assist 增加 `attachment_scope_id` 与 `attachment_refs`，inline `attachments` 保持兼容。混合请求共同受 8 个/单文件 6 MiB/总 48 MiB 边界，不重复引用计数。旧 inline 调用可在同一内部服务中创建仅该请求可见、finally 释放的临时表示。

原件和派生资产仅存在于独立临时区域，TTL 默认 30 分钟、单范围 8 件、派生单件 64 MiB、总存储 512 MiB、最多 2 个活动转换、有限等候；受限设置可调但不得变为无限。范围在页面生命周期内持有资源，活动请求有 pin/refcount 防清理竞争；显式释放标记关闭，最后活动引用释放后清理。重启清理本功能孤儿目录；已丢失引用返回 expired 而非重建为其他文件。默认单 Control 实例承载临时范围，跨重启不保证冻结轮次可恢复。不得把材料或文件名写进普通/工具审计日志。

### 4. Provider 能力与多模态读取

精确组合 resolver 公开 capability_source 与 image/native_pdf 状态；MiniMax 国内 M3 的图片能力已有官方文档，Kimi Code、OpenAI、Anthropic、Gemini 按具体模型/endpoint/协议核对。既有 custom images/files 勾选作为管理员声明保留，文件勾选本身不能启用未实现或未确认的原生 PDF。原生 PDF 优先 inline，避免新增上游上传资源生命周期；不支持时采用真实页图。未知能力明确限制，不自动更换模型。

最小工具为 `list_attachments`、`read_attachment_page(attachment_ref,page,view)`、`read_attachment_sheet(attachment_ref,sheet,range)`；单页与最多 200 cells 的受限范围，禁止路径/URL/Shell。读取结果为结构 JSON 和来源标记，加内部 media references。OpenAI-compatible 的 role=tool 仍只传文本，完整工具结果之后追加 server-generated user 多模态消息；Anthropic tool_result 可承载 image/document；Gemini 按实际支持用 multimodal function response 或 functionResponse 后追加 user inlineData。所有派生内容仍是 untrusted business reference，沿用 #203 的用户授权边界。

### 5. 每次调用同时检查 token 与实际 wire bytes

首次、后续工具、最终化和格式纠正共用预算器；统一识别图片/PDF 页数，工具 call/result 组保持完整。实际 protocol payload 的 JSON UTF-8 字节包含 base64 膨胀与 Schema/工具定义并执行上限；解析与工具读取继续消耗同一 Assist 总时限。省略媒体时保留其目录和明确覆盖提示，不能把必要工具图像全部省略后仍声称已经看过。

### 6. 浏览器临时生命周期与冻结请求

文件准备的各阶段映射为用户可理解的中英文状态；冻结轮次保存 refs/hash/revision，不存路径或 Provider 文件 ID。浏览器内引用统计覆盖 composer、visible/frozen rounds 和 in-flight 请求，删除 composer 文件不释放已冻结轮次所需资源。范围变化时取消准备和 Assist，发送 scope release；通知丢失由 TTL 兜底，迟到结果由 generation fencing 拒绝。页面重新挂载不恢复会话。Candidate 依旧仅经 Diff、Apply、Save、Run 生效。

### 7. 2026-10-22 剩余问题收尾

- inline 轻量兼容入口不依赖可选转换服务，但解析必须进入短生命周期 Python 子进程。使用标准 subprocess、受限 stdin/stdout 与固定解析类别，不传任意命令或持有 ORM/凭据；同进程有界解析许可，等待与执行共用原解析期限。超时/异常先终止并 wait/reap，失败后正常文件可恢复；不增加常驻服务或业务文件。
- sheet/range 的精确统计由原件解析器计算。`cell_summary` 标明 requested sheet/range、请求格数、非空格数、计数定义及公式/缓存信息；范围合并信息明确仅适用于相交区域。空串/空值与 0/false 分开，merged 的空占位不计非空，公式按原始公式格计数，不把缺失缓存变为零。
- 工具 4,000 字符上限、总预算和 D6 不变。截断按完整 cell 记录裁剪，保留原件身份、统计与其范围、合并范围适用范围、覆盖和遗漏数量；不得只留下 cells 前缀而丢掉原统计，必要时返回可补读信息。统计覆盖的范围与模型实际获得的 cell 明细分别标记。
- 提示规则使用上述程序事实作计数依据，保留页/sheet/range 引用；局部空合并列表不能代表全表，原始缓存缺失与派生重算值分开。严格 inline 提取只返回用户要求的已读原文；prepared 回答继续保留来源与未读范围，不授予附件指令、Secret 或执行权限。
- A4 使用独立合成原件、固定坐标真值、原始图像及人工可检查的显示预期，覆盖日期、货币、百分比、合并、公式缓存、颜色图表、嵌入图像和隐藏/打印范围。A16 固定原 87 字节材料及原问题，当前两模型重复验证提取、来源与注入边界；首败、修复后复验、未重跑范围分开记录。通过程序断言不替代模型事实判定。
- 复用固定私有测试验收编排与原卷/凭据，当前精确 PR HEAD CI 通过后更新同一环境；不使用普通 preview select 接管测试验收路径。PR 保持开放，Issue 保持开放，最终维护者 review 与用户验收另记。

## Risks / Trade-offs

### v0.4.4 四项兼容性修复

- Reader 在工具边界统一转换 DocumentConversionError，错误码只来自固定白名单；parse_timeout 使用既有 ai_tool_timeout，未知内容不回显。现有 HTTPException 的稳定码行为保持。
- scope 容量同时计入活动记录与 TTL 内关闭记录，首次受理即占位；单 Principal（跨 Adapter）最多 128 个，整体最多 512 个，既有最多 128 个活动 scope 的边界保持。已有 scope 在容量满时仍能关闭和回收，关闭记录不提前驱逐，TTL 到期归还容量。这限制单用户挤占，不宣称消除所有多用户全局竞争。
- 首次发送先 GET 校验服务端 refs，再冻结/清空草稿；跨等待期间的编辑、附件变化和账号/Adapter 切换取消旧发送，保留最新草稿。成功发送冻结 refs；Retry/Regenerate 继续校验其原引用。
- 不支持工具的配置不能读取 prepared 附件，请求在凭据解析和 Provider 调用前返回 ai_attachment_tools_unsupported（422），提示更换配置。无附件与旧 inline 咨询仍沿用原行为，不自动切换模型。

- CPU OCR 与每次冷进程加载有成本 → 有界并发、按页读取、预装离线权重，真实样本记录延时/峰值；不把全附件解析塞进首次请求。
- 结构提取与字体分页可能不完全准确 → 原件精确表格和真实图像并行保留，sheet映射只用真实 outline，公开 partial/unknown。
- 模型图片能力与格式预算因endpoint不同 → 精确 resolver 与 wire payload门禁，实际 MiniMax视觉验收只证明该组合；其他协议用真实编码合同回归并明确实测范围。
- 临时索引随 Control 重启丢失 → 过期错误和重新附加提示；不引入持久会话保证。

## Migration Plan

无业务数据库迁移。三个批次依序叠加 PR：临时表示/受限转换及 API；精确模型能力/读取工具/预算；界面生命周期与完整真实样本验收。每批保持既有无附件请求及 inline 请求兼容。部署新增隔离转换组件和独立临时存储；启动前检查镜像、字体、模型离线可用和资源额度。回滚先停止 AI 附件活动并释放本功能临时资产，再回退应用和转换组件，业务卷、Token、Master Key 和历史日志保持。
