## Context

见 proposal.md。当前已有 Node ESM harness、Java 编译路径、版本缓存、离线材料库及 cgroup v2 Attempt。数据库及消息 schema 多处限制三语言；依赖暂存固定 256 MiB，需要有界扩展支撑 Go SDK 冷编译。

## Goals / Non-Goals

**Goals:** 新语言复用现有执行权威、工作空间和脱敏机制；支持 SDK 依赖和代表性真实 Linux 执行。

**Non-Goals:** 不实现多文件 IDE、Go LSP 或 CGO；不重写旧语言运行机制；不为参考模板逐个配置云账号。

## Decisions

- TypeScript 使用固定 tsc 严格编译与 noEmitOnError，生成 ESM/source map 后复用 Node harness；浏览器加载平台类型声明。保存保留源码，环境检查及首次运行执行权威编译。
- Go 平台生成 package main 的 Context/harness 与 go.mod，用户提供 Handle(ctx *Context, input any) (any, error)，纯 Go 构建；提供 JSON 解码辅助方法支持结构体。固定工具链并关闭自动切换。
- Go 新增 goproxy 类型；缓存优先、仅按所选源下载。内置库接受标准 .mod/.info/.zip 模块材料，使用本地文件代理且关闭网络校验回退；摘要、任务快照、占用删除和配额沿用已有库。
- 在既有版本缓存中构建后原子发布，身份包含编译器、平台接口和目标架构。准备进程使用现有受限子进程运行方法。为编译引入有上限且纳入资源预算的暂存配置，并用 SDK 冷编译实测确定部署参数。
- 编辑器、AI 合同、所有源码流转入口一次补齐；原语言已保存源码不改写。新增工具链随 Worker 镜像提供，能力检测验证可用版本。
- 17 个参考模板新增 34 个变体；TypeScript 保留明确结构类型，Go 使用标准库或明确版本模块。沿用业务逻辑与输出合同，仅基础编译、元数据和广场交互验证。

## Risks / Trade-offs

- SDK 冷构建占用较高 → 限制并行构建和工作空间，以实测调整部署配置并保留拒绝超额请求行为。
- 前端与 Worker 类型能力差异 → 编辑器提供本地与平台基础类型，依赖完整类型以 Worker 检查为准。
- 包管理器隐式出网 → 内置源禁用代理回退、校验库访问和工具链下载，以离线失败测试验证。

## Migration Plan

新增 Alembic 迁移扩展语言与包类别约束，添加非默认 Go 内置源与 Go 在线源定义，不改旧资产。依次升级数据库、Control、Worker、Web；新语言只分配给兼容 Worker。回滚前停止并排空新语言任务，备份并显式处理新资产及 Go 材料；迁移遇到不兼容数据拒绝降级，不隐式删除。PR 和 CI 通过后保留隔离本地测试环境，不合并。
