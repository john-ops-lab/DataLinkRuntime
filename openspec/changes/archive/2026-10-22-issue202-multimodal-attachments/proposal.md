# Proposal

## Why

初始规划时 AI 附件把 PDF、Word 和工作簿压成文本，扫描件没有文字时被拒绝，图表、颜色、公式与页坐标丢失。现有 prepared 链路已补齐原件、结构、页图、范围与生命周期，保留初始设计及首败记录。

2026-10-22 收尾基线为 v0.6.0 / `5d71062342827fa2cd8b1723454c32066bd08c76`。仍需修复旧 inline 解析超时后线程继续运行、范围结果截断后统计/来源信息丢失，以及模型把局部合并信息或派生值扩大解释的问题；补齐 A4 日期/货币/百分比/嵌入图像组合与 A16 精确提取、来源及攻击样本验收。用户已授权实现、验收、同步本 Issue 与 OpenSpec、提交 PR；不得合并或关闭 Issue。

## What Changes

- 建立独立于业务 Managed Input 的临时附件范围：保留原始字节和哈希、派生页图/结构、范围所有权、TTL、活动引用、显式释放与失败清理；刷新或账号/Adapter 范围变化清空会话。
- 对 PDF 提供结构/OCR 和真实页图，保留空白页的原始序号；Office 使用经同样本评估的 Docling 与 LibreOffice 组合保留图表版式；XLSX/XLS 额外提供精确单元格、格式、合并、公式和缓存值来源。
- 每次请求先给有界目录/覆盖信息，再通过只读工具读取指定页、图片和 sheet/range；所有实际 Provider 调用继续计入当前总时限和文本/图像预算。
- 按实际 Provider、模型、endpoint 与协议验证图片/文件能力；支持 OpenAI-compatible、Anthropic Messages 和 Gemini 的真实多模态工具结果，不自动切换模型。
- UI 展示准备/转换/读取阶段、覆盖不足和可恢复错误；Retry/Regenerate 固定原始附件身份与哈希并保留现有迟到响应隔离。
- 分三个交付批次推进：附件表示与隔离转换、模型能力与有界读取、界面生命周期与完整真实样本验收。
- 收尾用短生命周期隔离解析进程替代 inline daemon thread；保持原限额与稳定错误码，超时后先实际回收再返回。
- sheet/range 返回程序计算的原件统计及精确范围；截断保留统计、来源、原缓存状态与可补读范围，模型不能凭部分 cells 自算整个文件，也不能将派生页重算值当原缓存。
- 只补受影响路径与明确缺口：真实当前 MiniMax/Kimi、A4/A16、代表性 Chrome 与人工业务链；未改路径的既有证据保留精确版本，不机械重跑全语言/全协议在线矩阵。

## Capabilities

### New Capabilities

- `ai-attachment-representations`: 原件、真实页图、结构与精确表格的定位和来源合同。
- `ai-attachment-lifecycle`: 临时附件所有权、引用、TTL、取消和资源边界。
- `ai-attachment-reading`: 目录、范围读取、多模态协议、预算和用户可见覆盖信息。

### Modified Capabilities

无现有 `openspec/specs/` capability 直接覆盖一次性 AI 附件；历史 AI change 作为兼容性依据，不改写历史合同或 Managed Input。

## Impact

影响 Control 的 AI 附件服务、Assist schemas、Provider 传输、预算与只读工具，Web AI 面板及中英文提示，并增加受限 CPU 文档转换组件和部署说明。保持现有 inline 附件请求兼容、既有数量/字节限额、手工 Apply/Save/Run、Provider 设置与凭据合同；不迁移业务数据库，不增加持久聊天、知识库或通用任务平台。新增临时存储与转换镜像需要明确资源、字体、权重和版本，回滚先停止附件请求并清理本功能临时资产，不删除业务卷或日志。

## 维护者确认与归档

2026-10-22，维护者独立评审通过后，PR #218 已合并到 main `fe5b03f`，合并后精确 SHA 的四项 CI 全部通过。维护者随后明确授权关闭 Issue #202、归档本 change 并发布 v0.6.1；这取代实施阶段的暂不合并／关闭限制。最终验收、历史失败和未测范围继续以收尾报告及 tasks 6.x／7 节为准；归档将三个 capability 同步为主规格，不新增产品行为。
