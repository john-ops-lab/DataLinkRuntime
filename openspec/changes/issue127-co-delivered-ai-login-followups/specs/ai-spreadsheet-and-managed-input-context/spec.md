## Purpose

定义 AI 助手处理电子表格附件与引用当前 Managed Input 配置时的可观察合同，使模型获得有界、明确且不越过 Artifact 安全边界的上下文。

## ADDED Requirements

### Requirement: AI 附件接受受控的 XLS 与 XLSX
系统 SHALL 在既有 AI 附件大小、数量、字符预算与解析超时内接受扩展名、声明 MIME 和文件签名一致的 XLS/XLSX，并将非空单元格按行列顺序转换为有界文本。

#### Scenario: 解析有效 XLSX
- **WHEN** 管理员为一次 AI 请求上传签名、扩展名和 MIME 一致且包含可读单元格的 XLSX
- **THEN** 系统把单元格文本以稳定的行列分隔加入该次 Provider 上下文，并沿用既有截断标记报告字符预算截断

#### Scenario: 解析有效旧式 XLS
- **WHEN** 管理员上传合法 BIFF XLS 且文件满足既有附件边界
- **THEN** 系统在内存中提取有界单元格文本，并不要求把文件写入临时目录

#### Scenario: 声明与文件签名不匹配
- **WHEN** 文件使用 XLS/XLSX 扩展名或 MIME 但实际签名不匹配
- **THEN** 系统以稳定的 `ai_attachment_type_unsupported` 拒绝请求，且不向 Provider 发送该附件

### Requirement: 电子表格解析不得执行活动内容
系统 MUST NOT 执行电子表格公式、宏、外部关系或嵌入指令；系统只可向 Provider 暴露已存储的显示值/单元格文本，并 SHALL 将其标记为不可信参考材料。

#### Scenario: 工作簿包含公式或外部关系
- **WHEN** 上传的工作簿包含公式、宏或外部关系
- **THEN** 系统不执行或获取任何活动内容，只保留安全可用的缓存显示值/文本或返回稳定解析错误

#### Scenario: 工作簿无可提取文本
- **WHEN** 工作簿在安全解析后没有非空可提取文本
- **THEN** 系统返回 `ai_attachment_no_text`，不伪造表格内容

### Requirement: 表格附件保持请求级无持久化边界
系统 MUST 在内存中处理 XLS/XLSX，并 MUST NOT 把原始文件、提取文本、公式、文件名或解析中间物写入数据库、临时文件或普通/审计日志。

#### Scenario: 解析成功、失败或超时
- **WHEN** 任一 XLS/XLSX 解析路径结束
- **THEN** 系统不留下请求外可复用的附件文件或解析文本，且超时/失败使用既有稳定错误合同

### Requirement: AI 只读取 Managed Input 安全元数据投影
当 Adapter 当前保存的输入来源为 `managed_files` 时，系统 SHALL 向 AI 上下文提供来源类型和按 ordinal 排序的公开文件标签，并 MUST NOT 读取 Blob、创建 Lease 或暴露 Artifact ID、storage key、Control/Worker 路径、Token、大小摘要以外的内部治理字段或文件内容。

#### Scenario: Adapter 保存了 Managed Files
- **WHEN** 管理员请求 AI 为该 Adapter 生成或修改代码
- **THEN** Provider 上下文只包含当前 revision 对应的安全文件标签及稳定三语言 Context 文件 API 指引

#### Scenario: ArtifactStore 不可用
- **WHEN** 当前 Managed Input 元数据仍可读取但 ArtifactStore 不可访问
- **THEN** AI 上下文组装不尝试打开 Blob，且不会因此产生文件下载或 Lease

#### Scenario: 输入来源不是 Managed Files
- **WHEN** 当前输入来源为 `none` 或 `json`
- **THEN** 系统不添加 Managed Files 专属读取提示，也不暗示模型存在文件内容

### Requirement: AI 不得声称看见 Managed Input 文件内容
系统 SHALL 明确告诉 Provider：文件列表只是当前保存配置的元数据，实际内容仅在 Adapter Execution 中通过稳定 Context API 可读；除非管理员另行作为该次 AI 附件上传，否则模型 MUST NOT 假设或复述文件内容。

#### Scenario: 用户要求根据已保存文件内容编写逻辑
- **WHEN** AI 仅收到 Managed Input 文件标签而没有对应附件文本
- **THEN** 生成建议只能使用文件 API 与元数据，不得编造工作簿列名、行值或其他文件内容
