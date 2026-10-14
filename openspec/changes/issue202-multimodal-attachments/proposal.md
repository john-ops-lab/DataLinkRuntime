# Proposal

## Why

当前 AI 附件把 PDF、Word 和工作簿压成文本，扫描件没有文字时被拒绝，图表、颜色、公式与页坐标丢失，不能完成 Issue #202 的业务场景。需要在当前临时 AI 调试与人工 Candidate/Diff/Apply 流程内，让实际支持视觉的模型收到真实图像和可定位的结构材料。

## What Changes

- 建立独立于业务 Managed Input 的临时附件范围：保留原始字节和哈希、派生页图/结构、范围所有权、TTL、活动引用、显式释放与失败清理；刷新或账号/Adapter 范围变化清空会话。
- 对 PDF 提供结构/OCR 和真实页图，保留空白页的原始序号；Office 使用经同样本评估的 Docling 与 LibreOffice 组合保留图表版式；XLSX/XLS 额外提供精确单元格、格式、合并、公式和缓存值来源。
- 每次请求先给有界目录/覆盖信息，再通过只读工具读取指定页、图片和 sheet/range；所有实际 Provider 调用继续计入当前总时限和文本/图像预算。
- 按实际 Provider、模型、endpoint 与协议验证图片/文件能力；支持 OpenAI-compatible、Anthropic Messages 和 Gemini 的真实多模态工具结果，不自动切换模型。
- UI 展示准备/转换/读取阶段、覆盖不足和可恢复错误；Retry/Regenerate 固定原始附件身份与哈希并保留现有迟到响应隔离。
- 分三个交付批次推进：附件表示与隔离转换、模型能力与有界读取、界面生命周期与完整真实样本验收。

## Capabilities

### New Capabilities

- `ai-attachment-representations`: 原件、真实页图、结构与精确表格的定位和来源合同。
- `ai-attachment-lifecycle`: 临时附件所有权、引用、TTL、取消和资源边界。
- `ai-attachment-reading`: 目录、范围读取、多模态协议、预算和用户可见覆盖信息。

### Modified Capabilities

无现有 `openspec/specs/` capability 直接覆盖一次性 AI 附件；历史 AI change 作为兼容性依据，不改写历史合同或 Managed Input。

## Impact

影响 Control 的 AI 附件服务、Assist schemas、Provider 传输、预算与只读工具，Web AI 面板及中英文提示，并增加受限 CPU 文档转换组件和部署说明。保持现有 inline 附件请求兼容、既有数量/字节限额、手工 Apply/Save/Run、Provider 设置与凭据合同；不迁移业务数据库，不增加持久聊天、知识库或通用任务平台。新增临时存储与转换镜像需要明确资源、字体、权重和版本，回滚先停止附件请求并清理本功能临时资产，不删除业务卷或日志。
