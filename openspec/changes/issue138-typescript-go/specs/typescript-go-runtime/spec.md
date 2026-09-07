## Purpose

让管理员在现有适配器工作台中使用 TypeScript 维护带类型的数据转换、使用 Go 复用模块和 SDK，同时保持 DLR 的输入输出、凭据、资源约束及资产交付体验一致。

## ADDED Requirements

### Requirement: 两种语言的统一适配器生命周期
系统 SHALL 允许创建、编辑、保存、Clone、模板复制、导入导出及运行 TypeScript 和 Go Adapter，保留原三种语言行为。只有具备对应工具链能力的 Worker SHALL 接受新语言执行。

#### Scenario: 五语言调度与资产流转
- **WHEN** 管理员保存或导入新语言代码并选择支持的 Worker
- **THEN** 源码与依赖形成不可变版本，Task、Schedule、Webhook 使用同一执行机制，Clone 与 ZIP 往返保持语言和源码。

### Requirement: 类型检查及编译
TypeScript SHALL 提供编辑器基础类型提示和 DLR Context 声明，并在 Worker 严格检查通过后运行；Go SHALL 先编译再运行。两者 SHALL 支持配置、凭据、受管文件、JSON 输入输出和日志。构建错误 SHALL 提供可定位诊断且不运行用户处理函数。

#### Scenario: 类型错误被拒绝
- **WHEN** TypeScript 代码包含类型错误或 Go 编译失败
- **THEN** 构建检查或执行明确失败并记录诊断，不生成可用构建缓存。

#### Scenario: Context 一致
- **WHEN** 两种语言读取配置、绑定凭据和输入文件并返回 JSON
- **THEN** 平台提供与现有语言等价的数据，日志沿用脱敏和输出上限。

### Requirement: 外部及内置依赖
TypeScript SHALL 复用 npm 源，Go SHALL 支持有明确版本的外部模块及平台 Go 源。两者 SHALL 支持内置离线材料，材料不足必须明确失败，不回退公网。工具链 SHALL 随 Worker 固定，不自动下载替代版本。

#### Scenario: 严格离线安装
- **WHEN** 选择内置源且材料缺少直接或传递依赖
- **THEN** 准备失败并给出原因，不访问公网补齐。

### Requirement: 有界构建与缓存
依赖准备、编译和执行 SHALL 受资源、时间、取消和清理约束。缓存 SHALL 区分源码、依赖、来源、工具链、平台接口以及 Go 目标架构；失败构建不得复用。

#### Scenario: 构建中取消或超限
- **WHEN** 编译期间发生取消、超时或资源不足
- **THEN** 终止相关进程、报告准确结果并清理或按现有恢复机制登记残留，后续运行不读取半成品。

### Requirement: 参考模板五语言覆盖
当前 17 个内置模板 SHALL 提供 TypeScript 和 Go 变体，包括官方接口支持的阿里云与腾讯云模板；用途、凭据和输出约定保持一致。模板 SHALL 通过目录、依赖、类型或编译检查并可在广场复制；不要求真实外部服务业务验收。

#### Scenario: 模板语言选择
- **WHEN** 用户打开现有任一模板
- **THEN** 可以选择 TypeScript 或 Go 并复制源码和依赖为独立适配器。
