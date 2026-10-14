# Design

## Context

现有 JSON Assist 携带 inline base64，附件解析是文本降级；PDF 页号因空白页跳过而丢失，XLSX 手工 XML 与 XLS/xlrd 无视觉。Provider 内部消息采用 OpenAI-compatible 中间表示，Anthropic/Gemini 再转换；工具结果仅为 JSON 字符串，预算只识别顶层图片。临时消息和冻结轮次已有 generation/scope fencing。动机与范围见 proposal.md。

## Goals / Non-Goals

**Goals:** 保持一次性调试，提供原件身份、真实视觉和精确表格交叉核对；大文件以目录及按页/范围读取进入既有有界只读工具链；对取消、超时和内存提供实际进程与存储证据。

**Non-Goals:** 不增加业务输入资产、数据库附件表、持久聊天、自动模型切换、自动 Save/Run、通用后台任务平台或 Docker socket 权限。

## Decisions

### 1. 复用成熟文档组件，按同样本选择组合

固定 Docling Serve CPU v1.37.0 和 Gotenberg v8.37.0 的 OCI digest，Docling 依赖及预装模型与该镜像一致；中文扫描采用已实测的 RapidOCR Torch `lang=[ch]` preset，运行时禁止下载模型。使用 Gotenberg 所含固定 LibreOffice 与 Noto CJK 字体，精确 XLSX 读取增加固定 openpyxl，XLS 复用 xlrd。部署清单记录实际版本、镜像、字体、权重与许可。

同一套中文表格、彩色图表、空白页、扫描件、带合并/格式/公式的 XLSX 与真实 BIFF XLS 已揭示：Docling 原生工作簿表示遗漏图表/格式，旧 XLS 无可用 LibreOffice；Office 渲染成 PDF 后图表可读，outline 可精确给出源 sheet 的 PDF 起始页。数字 PDF 的结构表格即使转换 status=success 也可能列错位，因此结构只能作辅助，并保留真实页图和部分覆盖说明。详细证据保存在私有验收材料，仓库文档仅记录可复现样本与结论。

### 2. 小型隔离转换服务，逐次监督实际进程

正式 Compose 提供仅内部可见的 CPU 文档转换服务，控制节点只调用受限 API。每次重解析由独立子进程/进程组执行，设墙钟、内存、PID、临时目录和输出字节硬限制，取消或期限到达后 kill/wait 并检查目录释放。Docling Serve Local 线程池客户端超时后仍持续转换的实测反证禁止其作为硬取消实现。Office 使用经过同样本验证的 Gotenberg/LibreOffice，必须验证官方取消能够停止对应转换；若 HTTP 包装不能保证，则复用同一固定镜像内 LibreOffice 的薄进程监督入口，保持相同版式结果。此选择依据取消证据完成，不能用断开 HTTP 代替终止。

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

## Risks / Trade-offs

- CPU OCR 与每次冷进程加载有成本 → 有界并发、按页读取、预装离线权重，真实样本记录延时/峰值；不把全附件解析塞进首次请求。
- 结构提取与字体分页可能不完全准确 → 原件精确表格和真实图像并行保留，sheet映射只用真实 outline，公开 partial/unknown。
- 模型图片能力与格式预算因endpoint不同 → 精确 resolver 与 wire payload门禁，实际 MiniMax视觉验收只证明该组合；其他协议用真实编码合同回归并明确实测范围。
- 临时索引随 Control 重启丢失 → 过期错误和重新附加提示；不引入持久会话保证。

## Migration Plan

无业务数据库迁移。三个批次依序叠加 PR：临时表示/受限转换及 API；精确模型能力/读取工具/预算；界面生命周期与完整真实样本验收。每批保持既有无附件请求及 inline 请求兼容。部署新增隔离转换组件和独立临时存储；启动前检查镜像、字体、模型离线可用和资源额度。回滚先停止 AI 附件活动并释放本功能临时资产，再回退应用和转换组件，业务卷、Token、Master Key 和历史日志保持。
