## Why

Issue #138 要求以 TypeScript 类型声明维护复杂转换，并复用 Go 接口逻辑与 SDK。两种语言必须覆盖现有适配器生命周期，而非仅增加选择项。

## What Changes

- 新增 TypeScript 严格检查、Node 执行及 Go Modules 编译运行，保持统一输入、输出、配置、凭据、文件和日志接口。
- Go 增加在线与内置离线模块源；TypeScript 复用 npm。Worker 工具链固定版本并按真实能力上报。
- API、数据库、消息校验、编辑器、AI、Clone、模板、ZIP 流转支持五种语言。
- 现有 17 个内置参考模板全部补两种语言，共 34 个变体；阿里云与腾讯云使用官方支持接口。
- 非目标：多文件工程导入、Go LSP、CGO、任意工具链自动下载、逐模板真实外部系统业务验收。

## Capabilities

### New Capabilities
- `typescript-go-runtime`: 两种语言的完整运行及产品契约，包含依赖与模板扩展。

### Modified Capabilities

无；本增量集中描述新语言契约，原三种语言保持兼容。

## Impact

影响 Control/Worker/Web、数据库语言与依赖类型约束、Worker 镜像和部署、模板资产及文档。新增迁移不重写已有适配器。先升级 Control 数据库及 Worker，再创建新语言资产；降级前必须排空新语言任务并处理新增资产及 Go 源，不删除用户数据。凭据沿用按执行注入与日志脱敏。提交 PR、CI 通过后提供隔离本地环境供用户验收，不自动合并。
